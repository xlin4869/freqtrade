import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from freqtrade.enums import SignalDirection
from freqtrade.rpc import RPC
from freqtrade.rpc.api_server.deps import get_config, get_rpc
from freqtrade.rpc.rpc import RPCException


logger = logging.getLogger(__name__)

router = APIRouter()


class SignalPayload(BaseModel):
    action: str                      # "buy", "sell", "exit", "exit_all"
    pair: str | None = None
    leverage: float | None = None
    # 仓位计算（二选一）: 直接指定 stake_amount，或用 capital × position_pct / 100 计算
    stake_amount: float | None = None
    capital: float | None = None     # 当前本金（USDT）
    position_pct: float | None = None  # 仓位占本金百分比（0-100）
    # 止盈止损（价格变动百分比，如 5.0 = 5%）
    sl_pct: float | None = None
    tp_pct: float | None = None


def _verify_webhook_token(token: str, config: dict) -> None:
    webhook_token = config.get("api_server", {}).get("webhook_token", "")
    if not webhook_token or token != webhook_token:
        raise HTTPException(status_code=403, detail="Invalid or missing webhook token")


def _calc_stake_amount(payload: SignalPayload) -> float | None:
    """从 capital + position_pct 计算仓位本金，或直接返回 stake_amount。"""
    if payload.stake_amount is not None:
        return payload.stake_amount
    if payload.capital is not None and payload.position_pct is not None:
        return round(payload.capital * payload.position_pct / 100, 2)
    return None


def _place_sl_tp_orders(rpc: RPC, trade, sl_pct: float | None, tp_pct: float | None) -> dict:
    """
    在交易所挂止损/止盈单。
    SL → STOP_MARKET 单（通过 exchange.create_stoploss）
    TP → 减仓 LIMIT 单（通过 exchange.create_order, reduceOnly=True）
    返回已挂单 ID 的字典。
    """
    result: dict = {}

    if not hasattr(rpc, "_freqtrade") or rpc._freqtrade is None:
        logger.warning("SL/TP: freqtradebot not available, skipping exchange orders")
        return result

    # dry_run 模式下不实际挂单
    if rpc._freqtrade.config.get("dry_run", True):
        logger.info("SL/TP: dry_run mode, skipping exchange orders")
        result["note"] = "dry_run: SL/TP orders not placed on exchange"
        return result

    exchange = rpc._freqtrade.exchange
    open_rate = trade.open_rate
    is_short = trade.is_short
    exit_side = trade.exit_side  # "sell" for long, "buy" for short

    # 获取实际持仓量（市价单立即成交，amount 应已设置）
    amount = trade.amount
    if not amount or amount <= 0:
        # 兜底：用仓位本金估算
        if trade.stake_amount and open_rate:
            amount = trade.stake_amount / open_rate
        else:
            logger.warning("SL/TP: trade amount is 0, cannot place orders")
            return result

    order_types = {"stoploss": "market"}  # → Binance futures STOP_MARKET

    # ── 止损单 ──────────────────────────────────────────────────────────────
    if sl_pct and sl_pct > 0:
        try:
            if is_short:
                sl_price = open_rate * (1 + sl_pct / 100)
            else:
                sl_price = open_rate * (1 - sl_pct / 100)

            sl_order = exchange.create_stoploss(
                pair=trade.pair,
                amount=amount,
                stop_price=sl_price,
                order_types=order_types,
                side=exit_side,
                leverage=trade.leverage,
            )
            sl_order_id = sl_order.get("id", "")
            trade.set_custom_data("sl_order_id", sl_order_id)
            result["sl_order_id"] = sl_order_id
            result["sl_price"] = sl_price
            logger.info(f"SL order placed: {sl_order_id} @ {sl_price} for {trade.pair}")
        except Exception as e:
            logger.error(f"Failed to place SL order: {e}")
            result["sl_error"] = str(e)

    # ── 止盈单 ──────────────────────────────────────────────────────────────
    if tp_pct and tp_pct > 0:
        try:
            if is_short:
                tp_price = open_rate * (1 - tp_pct / 100)
            else:
                tp_price = open_rate * (1 + tp_pct / 100)

            tp_order = exchange.create_order(
                pair=trade.pair,
                ordertype="limit",
                side=exit_side,
                amount=amount,
                rate=tp_price,
                leverage=trade.leverage,
                reduceOnly=True,
            )
            tp_order_id = tp_order.get("id", "")
            trade.set_custom_data("tp_order_id", tp_order_id)
            result["tp_order_id"] = tp_order_id
            result["tp_price"] = tp_price
            logger.info(f"TP order placed: {tp_order_id} @ {tp_price} for {trade.pair}")
        except Exception as e:
            logger.error(f"Failed to place TP order: {e}")
            result["tp_error"] = str(e)

    # 持久化 custom_data
    try:
        from freqtrade.persistence import Trade
        Trade.commit()
    except Exception:
        pass

    return result


@router.post("/signal", tags=["Trading"])
def receive_signal(
    payload: SignalPayload,
    token: str = Query(..., description="Webhook token for authentication"),
    rpc: RPC = Depends(get_rpc),
    config: dict = Depends(get_config),
):
    """
    接收 TradingView webhook 或前端手动开仓信号。

    请求格式：
    - 开多：{"action": "buy",  "pair": "BTC/USDT:USDT", "leverage": 5,
              "capital": 1000, "position_pct": 30, "sl_pct": 5, "tp_pct": 10}
    - 开空：{"action": "sell", "pair": "BTC/USDT:USDT", "leverage": 5,
              "stake_amount": 300, "sl_pct": 5, "tp_pct": 10}
    - 平指定币对：{"action": "exit",     "pair": "BTC/USDT:USDT"}
    - 全平：      {"action": "exit_all"}
    """
    _verify_webhook_token(token, config)

    action = payload.action.lower()
    stake_amount = _calc_stake_amount(payload)

    try:
        if action in ("buy", "sell"):
            if not payload.pair:
                raise HTTPException(
                    status_code=400, detail=f"'pair' is required for action '{action}'"
                )
            order_side = SignalDirection.LONG if action == "buy" else SignalDirection.SHORT

            trade = rpc._rpc_force_entry(
                payload.pair,
                None,
                order_side=order_side,
                enter_tag="tv_signal",
                leverage=payload.leverage,
                stake_amount=stake_amount,
            )

            if not trade:
                return {
                    "status": "error",
                    "action": action,
                    "pair": payload.pair,
                    "detail": f"Failed to enter {order_side} trade",
                }

            sl_tp_result = _place_sl_tp_orders(rpc, trade, payload.sl_pct, payload.tp_pct)

            return {
                "status": "ok",
                "action": action,
                "pair": payload.pair,
                "trade_id": trade.id,
                "open_rate": trade.open_rate,
                "stake_amount": trade.stake_amount,
                **sl_tp_result,
            }

        elif action == "exit":
            if not payload.pair:
                raise HTTPException(
                    status_code=400, detail="'pair' is required for action 'exit'"
                )
            from freqtrade.persistence import Trade
            trade = Trade.get_trades(
                [Trade.is_open.is_(True), Trade.pair == payload.pair]
            ).first()
            if not trade:
                raise HTTPException(
                    status_code=404, detail=f"No open trade found for pair {payload.pair}"
                )
            result = rpc._rpc_force_exit(str(trade.id))
            return {"status": "ok", "action": "exit", "pair": payload.pair, "result": result}

        elif action == "exit_all":
            result = rpc._rpc_force_exit("all")
            return {"status": "ok", "action": "exit_all", "result": result}

        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown action '{action}'. Use: buy, sell, exit, exit_all",
            )

    except RPCException as e:
        logger.error(f"Signal RPC error: {e}")
        raise HTTPException(status_code=502, detail=str(e))

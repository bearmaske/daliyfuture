import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

TZ_CN = timezone(timedelta(hours=8))


def now_cn() -> str:
    """Return current time in UTC+8, formatted as YYYY-MM-DD HH:MM:SS."""
    return datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")


class StateManager:
    def __init__(self, state_file: str, backup_file: str, initial_capital: float):
        self.state_file = state_file
        self.backup_file = backup_file
        self.initial_capital = initial_capital
        self._lock = threading.Lock()
        self.state = None

    def load(self) -> dict:
        with self._lock:
            if os.path.exists(self.state_file):
                try:
                    with open(self.state_file, "r") as f:
                        self.state = json.load(f)
                except (json.JSONDecodeError, IOError):
                    if os.path.exists(self.backup_file):
                        with open(self.backup_file, "r") as f:
                            self.state = json.load(f)
                    else:
                        self.state = self._default_state()
            else:
                self.state = self._default_state()
            return self.state

    def save(self):
        with self._lock:
            if os.path.exists(self.state_file):
                shutil.copy2(self.state_file, self.backup_file)
            with open(self.state_file, "w") as f:
                json.dump(self.state, f, indent=2, default=str)

    def add_position(
        self, symbol: str, side: str, entry_price: float, quantity: float,
        open_commission: float = 0.0,
    ) -> dict:
        with self._lock:
            pos = {
                "id": str(uuid.uuid4()),
                "symbol": symbol,
                "side": side,
                "entry_price": entry_price,
                "quantity": quantity,
                "highest_price": entry_price,
                "lowest_price": entry_price,
                "open_commission": open_commission,
                "opened_at": now_cn(),
            }
            self.state["positions"].append(pos)
        self.save()
        return pos

    def remove_position(self, position_id: str) -> Optional[dict]:
        with self._lock:
            for i, pos in enumerate(self.state["positions"]):
                if pos["id"] == position_id:
                    removed = self.state["positions"].pop(i)
                    break
            else:
                return None
        self.save()
        return removed

    def get_position_by_symbol(self, symbol: str) -> Optional[dict]:
        with self._lock:
            for pos in self.state["positions"]:
                if pos["symbol"] == symbol:
                    return pos
        return None

    def get_position_by_id(self, position_id: str) -> Optional[dict]:
        with self._lock:
            for pos in self.state["positions"]:
                if pos["id"] == position_id:
                    return pos
        return None

    def update_extreme_price(self, position_id: str, current_price: float):
        changed = False
        with self._lock:
            for pos in self.state["positions"]:
                if pos["id"] == position_id:
                    if current_price > pos["highest_price"]:
                        pos["highest_price"] = current_price
                        changed = True
                    if current_price < pos["lowest_price"]:
                        pos["lowest_price"] = current_price
                        changed = True
                    break
        if changed:
            self.save()

    def add_trade_history(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        exit_price: float,
        quantity: float,
        pnl: float,
        commission: float,
        opened_at: str,
    ):
        with self._lock:
            trade = {
                "id": str(uuid.uuid4()),
                "symbol": symbol,
                "side": side,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "quantity": quantity,
                "pnl": pnl,
                "commission": commission,
                "opened_at": opened_at,
                "closed_at": now_cn(),
            }
            self.state["trade_history"].append(trade)
        self.save()
        return trade

    def update_balance(self, amount: float):
        with self._lock:
            self.state["balance"] += amount
        self.save()

    def set_cooldown(self, hours: int):
        """Enter cooldown mode for the specified number of hours."""
        until = datetime.now(TZ_CN) + timedelta(hours=hours)
        with self._lock:
            self.state["cooldown_until"] = until.strftime("%Y-%m-%d %H:%M:%S")
        self.save()

    def is_in_cooldown(self) -> bool:
        """Check if the bot is currently in cooldown period."""
        cooldown_str = self.state.get("cooldown_until")
        if not cooldown_str:
            return False
        cooldown_until = datetime.strptime(cooldown_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_CN)
        if datetime.now(TZ_CN) < cooldown_until:
            return True
        # Cooldown expired, clear it
        with self._lock:
            self.state.pop("cooldown_until", None)
        self.save()
        return False

    def cooldown_remaining(self) -> Optional[str]:
        """Return remaining cooldown time as a human-readable string, or None."""
        cooldown_str = self.state.get("cooldown_until")
        if not cooldown_str:
            return None
        cooldown_until = datetime.strptime(cooldown_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_CN)
        remaining = cooldown_until - datetime.now(TZ_CN)
        if remaining.total_seconds() <= 0:
            return None
        hours, remainder = divmod(int(remaining.total_seconds()), 3600)
        minutes = remainder // 60
        return f"{hours}小时{minutes}分钟"

    @property
    def position_count(self) -> int:
        return len(self.state["positions"])

    @property
    def balance(self) -> float:
        return self.state["balance"]

    def sync_positions(self, remote_positions: list, remote_balance: float):
        """Sync local state with actual Testnet account positions and balance.
        remote_positions: list of dicts with symbol, side, entry_price, quantity."""
        with self._lock:
            local_symbols = {p["symbol"]: p for p in self.state["positions"]}
            remote_symbols = {p["symbol"]: p for p in remote_positions}

            # Remove local positions that no longer exist on Testnet
            removed = []
            kept = []
            for p in self.state["positions"]:
                if p["symbol"] in remote_symbols:
                    kept.append(p)
                else:
                    removed.append(p["symbol"])
            self.state["positions"] = kept

            # Add remote positions missing from local state
            # Update existing ones with remote data
            added = []
            for rp in remote_positions:
                if rp["symbol"] not in local_symbols:
                    pos = {
                        "id": str(uuid.uuid4()),
                        "symbol": rp["symbol"],
                        "side": rp["side"],
                        "entry_price": rp["entry_price"],
                        "quantity": rp["quantity"],
                        "highest_price": rp["entry_price"],
                        "lowest_price": rp["entry_price"],
                        "opened_at": rp.get("opened_at") or now_cn(),
                    }
                    self.state["positions"].append(pos)
                    added.append(rp["symbol"])
                else:
                    # Update quantity and entry_price from remote (source of truth)
                    local_pos = local_symbols[rp["symbol"]]
                    local_pos["quantity"] = rp["quantity"]
                    local_pos["entry_price"] = rp["entry_price"]

            # Always use remote balance
            self.state["balance"] = remote_balance

        self.save()
        return added, removed

    def _default_state(self) -> dict:
        return {
            "balance": self.initial_capital,
            "positions": [],
            "trade_history": [],
        }

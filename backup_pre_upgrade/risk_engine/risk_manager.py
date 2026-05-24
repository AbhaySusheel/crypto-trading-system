# risk_engine/risk_manager.py
import time
from datetime import datetime, date


class RiskManager:

    def __init__(self):
        # Position limits
        self.max_open_trades = 3              # Reduced from 5 (focus quality)
        self.max_notional_per_trade = 50      # Max $50 per trade

        # Risk per trade
        self.risk_amount = 5                   # $5 risk per trade (consistent)

        # Daily limits
        self.daily_loss_limit_usd = 15        # Stop after -$15 daily loss
        self.daily_profit_target = 30         # Optional: stop at +$30
        self.current_daily_pnl = 0
        self.current_day = date.today()

        # Circuit breaker
        self.consecutive_losses = 0
        self.max_consecutive_losses = 3
        self.cooldown_until = 0                # Timestamp until trading resumes

        # Total trades today
        self.trades_today = 0
        self.max_trades_per_day = 20

    def reset_daily(self):
        """Reset daily counters at start of new day"""
        today = date.today()
        if today != self.current_day:
            print(f"📅 NEW DAY: Resetting daily stats")
            self.current_day = today
            self.current_daily_pnl = 0
            self.trades_today = 0
            self.consecutive_losses = 0
            self.cooldown_until = 0

    def can_trade(self, open_trades_count):
        self.reset_daily()

        # Check cooldown (after consecutive losses)
        if time.time() < self.cooldown_until:
            remaining = int(self.cooldown_until - time.time())
            print(f"🛑 CIRCUIT BREAKER ACTIVE: {remaining}s remaining")
            return False

        # Max open positions
        if open_trades_count >= self.max_open_trades:
            print(f"🛑 MAX OPEN TRADES ({self.max_open_trades}) REACHED")
            return False

        # Daily loss limit
        if self.current_daily_pnl <= -self.daily_loss_limit_usd:
            print(f"🛑 DAILY LOSS LIMIT HIT: ${self.current_daily_pnl:.2f}")
            return False

        # Max trades per day
        if self.trades_today >= self.max_trades_per_day:
            print(f"🛑 MAX DAILY TRADES ({self.max_trades_per_day}) REACHED")
            return False

        # Daily profit target (optional, comment out if you don't want this)
        if self.current_daily_pnl >= self.daily_profit_target:
            print(f"🎯 DAILY PROFIT TARGET HIT: ${self.current_daily_pnl:.2f}")
            return False

        return True

    def position_size(self, balance, entry_price, stop_loss):
        """
        Calculate position size based on risk amount.
        risk_amount = (entry - stop_loss) * qty
        => qty = risk_amount / |entry - stop_loss|
        """
        risk_per_unit = abs(entry_price - stop_loss)

        if risk_per_unit == 0:
            return 0

        qty = self.risk_amount / risk_per_unit

        # Cap by max notional value
        max_qty = self.max_notional_per_trade / entry_price
        final_qty = min(qty, max_qty)

        return round(final_qty, 6)

    def update_pnl(self, pnl):
        """Update daily PnL and consecutive loss counter"""
        self.reset_daily()
        self.current_daily_pnl += pnl
        self.trades_today += 1

        if pnl < 0:
            self.consecutive_losses += 1
            print(f"📉 Consecutive losses: {self.consecutive_losses}")

            if self.consecutive_losses >= self.max_consecutive_losses:
                # 30-minute cooldown
                self.cooldown_until = time.time() + 1800
                print(f"🛑 CIRCUIT BREAKER TRIGGERED: 30min cooldown")
        else:
            self.consecutive_losses = 0
            print(f"📈 Win streak resumed")

    def get_status(self):
        return {
            "daily_pnl": round(self.current_daily_pnl, 2),
            "trades_today": self.trades_today,
            "consecutive_losses": self.consecutive_losses,
            "cooldown_remaining": max(0, int(self.cooldown_until - time.time()))
        }
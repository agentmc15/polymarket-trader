"""Order execution service."""
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services.polymarket.client import ClobClientWrapper
from app.strategies.base import Signal


@dataclass
class ExecutionResult:
    """Result of order execution."""

    success: bool
    order_id: str | None = None
    fill_price: float | None = None
    fill_size: float | None = None
    error: str | None = None
    timestamp: datetime | None = None
    metadata: dict[str, Any] | None = None


class OrderExecutor:
    """Service for executing orders on Polymarket."""

    def __init__(self, client: ClobClientWrapper) -> None:
        """Initialize the executor.

        Args:
            client: CLOB client for order execution.
        """
        self.client = client

    async def execute_signal(
        self,
        signal: Signal,
        dry_run: bool = False,
    ) -> ExecutionResult:
        """Execute a trading signal.

        Args:
            signal: Trading signal to execute.
            dry_run: If True, simulate execution without placing order.

        Returns:
            ExecutionResult: Execution result.
        """
        if dry_run:
            return ExecutionResult(
                success=True,
                order_id="dry-run",
                fill_price=signal.price,
                fill_size=signal.size,
                timestamp=datetime.utcnow(),
                metadata={"dry_run": True},
            )

        try:
            # Get current price if not specified
            price = signal.price
            if price is None:
                price = await self.client.get_price(signal.token_id, signal.side)

            # Create order
            order = await self.client.create_order(
                token_id=signal.token_id,
                price=price,
                size=signal.size or 0.0,
                side=signal.side,
            )

            # Submit order
            response = await self.client.post_order(order)

            return ExecutionResult(
                success=True,
                order_id=response.get("orderID"),
                fill_price=price,
                fill_size=signal.size,
                timestamp=datetime.utcnow(),
                metadata=response,
            )

        except Exception as e:
            return ExecutionResult(
                success=False,
                error=str(e),
                timestamp=datetime.utcnow(),
            )

    async def cancel_order(self, order_id: str) -> ExecutionResult:
        """Cancel an existing order.

        Args:
            order_id: Order ID to cancel.

        Returns:
            ExecutionResult: Cancellation result.
        """
        try:
            response = await self.client.cancel_order(order_id)
            return ExecutionResult(
                success=True,
                order_id=order_id,
                timestamp=datetime.utcnow(),
                metadata=response,
            )
        except Exception as e:
            return ExecutionResult(
                success=False,
                order_id=order_id,
                error=str(e),
                timestamp=datetime.utcnow(),
            )

    async def get_fill_status(self, order_id: str) -> dict[str, Any]:
        """Get fill status for an order.

        Args:
            order_id: Order ID to check.

        Returns:
            dict: Order status and fill information.
        """
        orders = await self.client.get_orders()
        for order in orders:
            if order.get("id") == order_id:
                return order
        return {"status": "not_found"}

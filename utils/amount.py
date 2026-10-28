"""
Amount handling utilities for economic operations.

Provides standardized rounding and validation for currency and crypto amounts.
All economic values should use these utilities to ensure consistency.
"""

from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN, getcontext, localcontext
from typing import Optional, Tuple

# Set decimal context precision high enough for large economic values
# Default is 28, but we need at least 38 for large supply calculations
getcontext().prec = 38

# Precision constants
CURRENCY_PRECISION = Decimal("0.01")  # 2 decimal places
CRYPTO_PRECISION = Decimal("0.00000001")  # 8 decimal places

# Minimum amounts
MIN_CURRENCY_AMOUNT = Decimal("0.01")
MIN_CRYPTO_AMOUNT = Decimal("0.00000001")


def _working_precision(amount: Decimal, places: int) -> int:
    """Digits a context needs to quantize ``amount`` to ``places`` decimals.

    No fixed precision is enough forever: this economy's supply grows without
    bound, and quantizing a balance to the cent needs one digit per integer
    digit plus the decimals. Once total supply passed 1e36 the 38-digit global
    context could no longer hold the result of ``round_currency``, so every
    wallet write raised ``InvalidOperation`` and gambling stopped. This returns
    the digits the number actually needs, with head room, and never less than
    the global precision so ordinary amounts behave exactly as before.
    """
    if not amount.is_finite():
        return getcontext().prec
    integer_digits = max(amount.adjusted() + 1, 1) if amount else 1
    return max(getcontext().prec, integer_digits + places + 4)


class AmountUtils:
    """Utility class for handling monetary amounts with proper rounding."""

    @staticmethod
    def quantize(
        amount: Decimal | int | float | str,
        exponent: Decimal,
        rounding: Optional[str] = None,
    ) -> Decimal:
        """Quantize to ``exponent`` at whatever precision the value needs.

        ``Decimal.quantize`` raises ``InvalidOperation`` when the result would
        need more digits than the active context keeps, which is how a supply
        past 1e36 broke ``round_currency`` and with it every wallet write. The
        context is widened locally to fit the number, so two (or eight)
        decimals stay exact at any magnitude. ``rounding=None`` keeps the
        active context's rounding, matching a bare ``quantize`` call.
        """
        if not isinstance(amount, Decimal):
            amount = Decimal(str(amount))
        places = max(0, -exponent.as_tuple().exponent)
        with localcontext() as ctx:
            ctx.prec = _working_precision(amount, places)
            if rounding is None:
                return amount.quantize(exponent)
            return amount.quantize(exponent, rounding=rounding)

    @staticmethod
    def round_currency(amount: Decimal) -> Decimal:
        """
        Round a currency amount to 2 decimal places using ROUND_HALF_UP.
        
        This is the standard financial rounding method where 0.005 rounds up to 0.01.
        Use this for regular amount inputs (not 'all'/'max').
        
        Args:
            amount: The amount to round
            
        Returns:
            Amount rounded to 2 decimal places
        """
        return AmountUtils.quantize(amount, CURRENCY_PRECISION, rounding=ROUND_HALF_UP)
    
    @staticmethod
    def truncate_currency(amount: Decimal) -> Decimal:
        """
        Truncate a currency amount to 2 decimal places using ROUND_DOWN.
        
        Use this for 'all'/'max' keywords to ensure the amount never exceeds
        the available balance (prevents "insufficient funds" edge cases).
        
        Args:
            amount: The amount to truncate
            
        Returns:
            Amount truncated to 2 decimal places
        """
        return AmountUtils.quantize(amount, CURRENCY_PRECISION, rounding=ROUND_DOWN)
    
    @staticmethod
    def round_crypto(amount: Decimal) -> Decimal:
        """
        Round a crypto amount to 8 decimal places using ROUND_HALF_UP.
        
        Use this for regular crypto amount inputs (not 'all'/'max').
        
        Args:
            amount: The amount to round
            
        Returns:
            Amount rounded to 8 decimal places
        """
        return AmountUtils.quantize(amount, CRYPTO_PRECISION, rounding=ROUND_HALF_UP)
    
    @staticmethod
    def truncate_crypto(amount: Decimal) -> Decimal:
        """
        Truncate a crypto amount to 8 decimal places using ROUND_DOWN.
        
        Use this for 'all'/'max' keywords in crypto operations.
        
        Args:
            amount: The amount to truncate
            
        Returns:
            Amount truncated to 8 decimal places
        """
        return AmountUtils.quantize(amount, CRYPTO_PRECISION, rounding=ROUND_DOWN)
    
    @staticmethod
    def validate_currency_minimum(amount: Decimal) -> Tuple[bool, str]:
        """
        Validate that a currency amount meets the minimum requirement.
        
        Args:
            amount: The amount to validate
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        if amount < MIN_CURRENCY_AMOUNT:
            return False, f"Amount must be at least {MIN_CURRENCY_AMOUNT}."
        return True, ""
    
    @staticmethod
    def validate_crypto_minimum(amount: Decimal) -> Tuple[bool, str]:
        """
        Validate that a crypto amount meets the minimum requirement.
        
        Args:
            amount: The amount to validate
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        if amount < MIN_CRYPTO_AMOUNT:
            return False, f"Amount must be at least {MIN_CRYPTO_AMOUNT}."
        return True, ""
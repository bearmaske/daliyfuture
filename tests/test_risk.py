import pytest
from risk import should_stop_loss


def test_long_stop_loss_triggered():
    result = should_stop_loss(
        side="LONG",
        highest_price=100.0,
        lowest_price=90.0,
        current_price=96.5,
        long_stop=0.03,
        short_stop=0.05,
    )
    assert result is True


def test_long_stop_loss_not_triggered():
    result = should_stop_loss(
        side="LONG",
        highest_price=100.0,
        lowest_price=90.0,
        current_price=98.0,
        long_stop=0.03,
        short_stop=0.05,
    )
    assert result is False


def test_short_stop_loss_triggered():
    result = should_stop_loss(
        side="SHORT",
        highest_price=110.0,
        lowest_price=100.0,
        current_price=105.5,
        long_stop=0.03,
        short_stop=0.05,
    )
    assert result is True


def test_short_stop_loss_not_triggered():
    result = should_stop_loss(
        side="SHORT",
        highest_price=110.0,
        lowest_price=100.0,
        current_price=103.0,
        long_stop=0.03,
        short_stop=0.05,
    )
    assert result is False

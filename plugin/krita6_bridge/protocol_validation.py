"""Dependency-free scalar validation shared by bridge command contracts."""

import math
import re

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class BridgeError(Exception):
    def __init__(self, code, message, effect="none"):
        super().__init__(message)
        self.code = code
        self.message = message
        self.effect = effect


def _invalid(message):
    raise BridgeError("INVALID_REQUEST", message)


def validate_id(value, field="identifier"):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _invalid(field + " must be a bounded identifier")
    return value


def _object(value, allowed, required=()):
    if not isinstance(value, dict):
        _invalid("Expected a JSON object")
    if set(value) - set(allowed):
        _invalid("Unknown object fields")
    if set(required) - set(value):
        _invalid("Missing required fields")
    return dict(value)


def _integer(value, low, high, field):
    if type(value) is not int or not low <= value <= high:
        _invalid(field + " is outside its integer bounds")
    return value


def _number(value, low=None, high=None, field="number"):
    if type(value) not in (int, float):
        _invalid(field + " must be finite numeric data")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or (low is not None and value < low) or (high is not None and value > high):
        _invalid(field + " is outside its finite numeric bounds")
    return float(value)


def _string(value, low, high, field):
    if not isinstance(value, str) or not low <= len(value) <= high or "\x00" in value:
        _invalid(field + " must be bounded text without NUL")
    return value


def _point(value, integer=False):
    if not isinstance(value, list) or len(value) != 2:
        _invalid("Coordinates must be pairs")
    if integer:
        return [_integer(v, -(2**31), 2**31 - 1, "coordinate") for v in value]
    return [_number(v, field="coordinate") for v in value]

"""NFC reader abstraction for the local attendance prototype."""

from .reader import NFCReaderService
from .simulator import simulate_nfc_scan

__all__ = ["NFCReaderService", "simulate_nfc_scan"]

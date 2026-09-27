def simulate_nfc_scan(uid: str) -> str:
    """Normalize and validate a simulated UID from the developer panel."""
    cleaned = ''.join(ch for ch in str(uid or '').upper() if ch.isalnum())
    return cleaned[:32]

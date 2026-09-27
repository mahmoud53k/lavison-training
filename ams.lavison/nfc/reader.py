class NFCReaderService:
    """Simple abstraction for future HID/PC/SC integration.

    For the prototype, the reader can be simulated or driven from a keyboard
    input. The service is deliberately small and easy to replace with a real
    PC/SC implementation later.
    """

    def __init__(self, mode: str = "keyboard"):
        self.mode = mode

    def read_uid(self, value: str | None = None) -> str | None:
        if value is None:
            return None
        cleaned = ''.join(ch for ch in str(value).upper() if ch.isalnum())
        return cleaned or None

    def get_mode_label(self) -> str:
        return "Keyboard/HID" if self.mode == "keyboard" else "PC/SC"

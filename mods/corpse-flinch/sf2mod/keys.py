"""Watch the game's use key (E by default) and turn presses into taps.

Two sources, preferred in this order:

  game -- SFPlayerController.bPressingUsedKey (0x690, mask 0x40).  This is the
          flag the game itself sets while the interact key is held, so it follows
          rebinds and stays quiet while a chat box has the keyboard.
  os   -- GetAsyncKeyState(VK_E).  Only used until the game flag has been seen
          once in this session; after that a press the game does not know about
          (typing in chat, alt-tabbed) must not move a body around.

`bIgnoreInput` (same word, mask 0x400) suppresses taps: while the game is in a
menu or a text field it is exactly the state we must not react to.
"""
import ctypes
import struct
import time

FLAGS = 0x690                 # SFPlayerController bool word (build specific)
USED_KEY = 0x40               # bPressingUsedKey
IGNORE_INPUT = 0x400          # bIgnoreInput
VK_E = 0x45

TAP_GAP = 0.25                # seconds between two accepted taps


class Keys:
    def __init__(self, bridge, config):
        self.bridge = bridge
        self.vk = int(config.get('drag_key', VK_E))
        self.native_ok = False       # the game flag has been seen rising
        self.native = False
        self.os_down = False
        self.last = 0.0
        self.count = 0
        self.word = None             # raw bool word, for the log
        self.source = None           # which source produced the newest tap
        try:
            self.user32 = ctypes.windll.user32
        except AttributeError:       # not on Windows: fall back to the game flag
            self.user32 = None

    # ---------------------------------------------------------------- reading
    def flags(self, controller):
        """(used_key, ignore_input) as the game sees them right now."""
        self.word = None
        if not controller:
            return False, True                   # no controller: nothing to press
        try:
            word = struct.unpack('<I', self.bridge.read(controller + FLAGS, 4))[0]
        except OSError:
            return False, True
        self.word = word
        return bool(word & USED_KEY), bool(word & IGNORE_INPUT)

    def os_state(self):
        if self.user32 is None:
            return False
        return bool(self.user32.GetAsyncKeyState(self.vk) & 0x8000)

    # ------------------------------------------------------------------- edge
    def poll(self, controller):
        """A list of tap sources seen since the previous poll (empty usually)."""
        used, blocked = self.flags(controller)
        rising = used and not self.native
        if rising:
            self.native_ok = True
        down = self.os_state()
        os_rising = down and not self.os_down
        self.native, self.os_down = used, down

        if rising:
            source = 'game'
        elif os_rising and not self.native_ok:
            source = 'os'
        else:
            return []
        if blocked or time.monotonic() - self.last < TAP_GAP:
            return []
        self.last = time.monotonic()
        self.count += 1
        self.source = source
        return [source]

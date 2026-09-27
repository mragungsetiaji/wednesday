"""Public keys Wednesday trusts for downloaded plugins: key id -> base64 of the 32-byte raw Ed25519 key.

A plugin from the licence server is only unpacked when its manifest is signed by one
of these (see ``plugin_install.py``). Public keys are safe to publish; the private
key never leaves whoever signs.
"""

TRUSTED: dict[str, str] = {
    "momentum-2026": "DP/JTo+dAgfsgYwklROQ6p4sji/iUJt9lzQSfcXsTlM=",
}

"""The kitchen kiosk — a LAN-free, tailnet-only web surface for the family.

Everything under this package is optional: it is imported by the shell only when
`VOS_KIOSK_ENABLED` is set, and its dependencies live in the `kiosk` extra. The
daemon must start, run and pass its tests without either.
"""

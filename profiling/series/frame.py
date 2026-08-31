"""ziskos input framing: LE64(len) + witness + zero-pad to 8.

SP1 does NOT want this — it reads the witness buffer verbatim. Handing SP1 a
framed buffer does not fail, it parses as garbage: every block returned the same
~8,000 cycles and was recorded as a measurement.
"""
import struct, sys
d = open(sys.argv[1], 'rb').read()
open(sys.argv[2], 'wb').write(struct.pack('<Q', len(d)) + d + b'\x00' * ((-(8 + len(d))) % 8))

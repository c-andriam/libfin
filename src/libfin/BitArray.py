class BitArray:
    """
    This is a minimal native python replacement for the bitarray module that was used to interpret
    the iso binary bitmap. This class has been optimized to use native python bitwise operations.
    """
    def __init__(self, endian='big'):
        self.endian = endian
        self.bytes = b''

    def frombytes(self, array_bytes: bytes):
        self.bytes = array_bytes

    def tobytes(self) -> bytes:
        return self.bytes

    def tolist(self) -> list:
        if not self.bytes:
            return []

        byte_len = len(self.bytes)
        num_bits = byte_len * 8
        result = [False] * num_bits

        if self.endian == 'little':
            for i, b in enumerate(self.bytes):
                for j in range(8):
                    result[i * 8 + j] = bool(b & (1 << j))
        else:
            val = int.from_bytes(self.bytes, byteorder='big')
            for i in range(num_bits):
                result[i] = bool((val >> (num_bits - 1 - i)) & 1)

        return result

    def fromlist(self, bytelist: list):
        if not bytelist:
            self.bytes = b''
            return

        num_bits = len(bytelist)
        num_bytes = (num_bits + 7) // 8

        val = 0
        for i, bit in enumerate(bytelist):
            if bit:
                val |= (1 << (num_bits - 1 - i))

        self.bytes = val.to_bytes(num_bytes, byteorder='big')

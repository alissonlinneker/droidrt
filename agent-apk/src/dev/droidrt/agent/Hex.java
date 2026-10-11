package dev.droidrt.agent;

/** Lowercase hex encoding and strict decoding. */
final class Hex {
    private static final char[] DIGITS = "0123456789abcdef".toCharArray();

    private Hex() {
    }

    static String encode(byte[] data) {
        char[] out = new char[data.length * 2];
        for (int i = 0; i < data.length; i++) {
            out[2 * i] = DIGITS[(data[i] >> 4) & 0xf];
            out[2 * i + 1] = DIGITS[data[i] & 0xf];
        }
        return new String(out);
    }

    /** Decodes hex text, or returns null if it is not valid hex of even length. */
    static byte[] decode(String hex) {
        if (hex == null || (hex.length() & 1) != 0) {
            return null;
        }
        byte[] out = new byte[hex.length() / 2];
        for (int i = 0; i < out.length; i++) {
            int hi = Character.digit(hex.charAt(2 * i), 16);
            int lo = Character.digit(hex.charAt(2 * i + 1), 16);
            if (hi < 0 || lo < 0) {
                return null;
            }
            out[i] = (byte) ((hi << 4) | lo);
        }
        return out;
    }
}

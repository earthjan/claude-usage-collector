import os
import stat
import tempfile
import unittest

from usage_collector import hashing


class HashValueTest(unittest.TestCase):
    def test_matches_rfc4231_vector_truncated_to_12_hex(self):
        # RFC 4231 test case 2: HMAC-SHA256("Jefe", "what do ya want for nothing?")
        self.assertEqual(
            hashing.hash_value(b"Jefe", "what do ya want for nothing?"),
            "5bdcc146bf60",
        )

    def test_different_salts_give_different_hashes(self):
        self.assertNotEqual(
            hashing.hash_value(b"a" * 32, "session"),
            hashing.hash_value(b"b" * 32, "session"),
        )


class SaltFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "salt")

    def tearDown(self):
        self.tmp.cleanup()

    def test_created_salt_is_32_bytes_and_private(self):
        salt = hashing.create_salt(self.path)
        self.assertEqual(len(salt), 32)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(hashing.load_salt(self.path), salt)

    def test_create_keeps_existing_salt(self):
        first = hashing.create_salt(self.path)
        self.assertEqual(hashing.create_salt(self.path), first)

    def test_load_missing_salt_raises(self):
        with self.assertRaises(hashing.SaltError):
            hashing.load_salt(self.path)


if __name__ == "__main__":
    unittest.main()

import unittest

from werkzeug.security import check_password_hash, generate_password_hash

from app import PASSWORD_HASH_METHOD


class PasswordHashTest(unittest.TestCase):
    def test_password_hash_uses_centos_compatible_method(self):
        password_hash = generate_password_hash("test-password", method=PASSWORD_HASH_METHOD)

        self.assertTrue(password_hash.startswith("pbkdf2:sha256:"))
        self.assertTrue(check_password_hash(password_hash, "test-password"))


if __name__ == "__main__":
    unittest.main()

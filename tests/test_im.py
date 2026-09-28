import base64
import hashlib
import hmac
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from fastapi import HTTPException
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from app import im


class IMVerificationTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'FEISHU_VERIFICATION_TOKEN': 'verification-test',
                         'FEISHU_ENCRYPT_KEY': 'encryption-test', 'DINGTALK_APP_SECRET': 'ding-test'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def headers(self, raw):
        stamp, nonce = str(int(time.time())), 'test-nonce'
        return {'x-lark-request-timestamp': stamp, 'x-lark-request-nonce': nonce,
                'x-lark-signature': hashlib.sha256((stamp + nonce + 'encryption-test').encode() + raw).hexdigest()}

    def test_feishu_signed_body_and_tamper(self):
        raw = json.dumps({'schema': '2.0', 'header': {'token': 'verification-test'}}).encode()
        self.assertEqual(im.verify_feishu(raw, self.headers(raw))['schema'], '2.0')
        with self.assertRaises(HTTPException):
            im.verify_feishu(raw + b' ', self.headers(raw))

    def test_feishu_aes_and_challenge(self):
        value = {'type': 'url_verification', 'token': 'verification-test', 'challenge': 'challenge-123'}
        padder = padding.PKCS7(128).padder()
        plaintext = padder.update(json.dumps(value).encode()) + padder.finalize()
        iv = bytes(range(16))
        cipher = Cipher(algorithms.AES(hashlib.sha256(b'encryption-test').digest()), modes.CBC(iv)).encryptor()
        encrypted = base64.b64encode(iv + cipher.update(plaintext) + cipher.finalize()).decode()
        self.assertEqual(im.verify_feishu(json.dumps({'encrypt': encrypted}).encode(), {}), value)
        with self.assertRaises(HTTPException):
            im.decrypt_feishu(encrypted, 'wrong-key')

    def test_challenge_requires_token(self):
        with self.assertRaises(HTTPException):
            im.verify_feishu(b'{"type":"url_verification","challenge":"x"}', {})

    def test_missing_signature_and_expired(self):
        raw = b'{"schema":"2.0","header":{"token":"verification-test"}}'
        with self.assertRaises(HTTPException):
            im.verify_feishu(raw, {})
        headers = self.headers(raw)
        headers['x-lark-request-timestamp'] = '1'
        with self.assertRaises(HTTPException):
            im.verify_feishu(raw, headers)

    def test_dingtalk_signature_and_time_window(self):
        stamp = str(int(time.time() * 1000))
        signature = base64.b64encode(hmac.new(b'ding-test', (stamp + '\nding-test').encode(), hashlib.sha256).digest()).decode()
        im.verify_dingtalk({'timestamp': stamp, 'sign': signature})
        for headers in ({'timestamp': stamp, 'sign': 'invalid'}, {'timestamp': '1', 'sign': signature}, {}):
            with self.assertRaises(HTTPException):
                im.verify_dingtalk(headers)

    def test_dingtalk_endpoint_allowlist(self):
        self.assertTrue(im.strict_dingtalk_url('https://oapi.dingtalk.com/robot/send?access_token=test'))
        for url in ('http://oapi.dingtalk.com/robot/send', 'https://oapi.dingtalk.com.evil.test/robot/send',
                    'https://evil.test/robot/send', 'https://user@oapi.dingtalk.com/robot/send',
                    'https://oapi.dingtalk.com:444/robot/send', 'https://127.0.0.1/robot/send',
                    'https://oapi.dingtalk.com/other', 'https://oapi.dingtalk.com/robot/send#x'):
            self.assertFalse(im.strict_dingtalk_url(url), url)

    def test_unicode_token_rejected_without_server_error(self):
        with self.assertRaises(HTTPException) as raised:
            im.verify_feishu(json.dumps({'type': 'url_verification', 'token': '错误', 'challenge': 'x'}).encode(), {})
        self.assertEqual(raised.exception.status_code, 403)

    def test_invalid_payloads(self):
        for raw in (b'[]', b'null', b'{', b'"text"'):
            with self.assertRaises(HTTPException):
                im._object(raw)


if __name__ == '__main__':
    unittest.main()

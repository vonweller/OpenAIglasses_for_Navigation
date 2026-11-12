import unittest

import torch

from aiglasses.yoloe_backend import _regular_tensor


class YoloEBackendTests(unittest.TestCase):
    def test_inference_tensor_is_converted_before_caching(self):
        with torch.inference_mode():
            inference_tensor = torch.ones((1, 2, 3))

        self.assertTrue(torch.is_inference(inference_tensor))
        cached = _regular_tensor(inference_tensor, dtype=torch.float16)

        self.assertFalse(torch.is_inference(cached))
        self.assertEqual(cached.dtype, torch.float16)
        self.assertIsInstance(cached._version, int)


if __name__ == "__main__":
    unittest.main()

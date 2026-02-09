# epd_helper.py

import logging
import mmap
import os

from PIL import Image

logger = logging.getLogger(__name__)


class _FBInfo:
    def __init__(self, width, height):
        self.width = width
        self.height = height


class EPDHelper:
    def __init__(self, epd_type=None, fb_path="/dev/fb0"):
        # epd_type is kept for compatibility but unused for framebuffer output.
        self.fb_path = os.environ.get("BJORN_FB", fb_path)
        self.width = 0
        self.height = 0
        self.bits_per_pixel = 0
        self.line_length = 0
        self.fb_fd = None
        self.fb_map = None
        self.enabled = False
        try:
            self.width, self.height = self._read_fb_size()
            self.bits_per_pixel = self._read_fb_bpp()
            self.line_length = self._read_fb_stride()
            self.epd = _FBInfo(self.width, self.height)
            self._open_fb()
            self.enabled = True
        except Exception as e:
            logger.warning(f"Framebuffer unavailable ({self.fb_path}): {e}. Continuing without local display.")
            self.epd = _FBInfo(0, 0)

    def _read_fb_size(self):
        size_path = "/sys/class/graphics/fb0/virtual_size"
        try:
            with open(size_path, "r") as f:
                raw = f.read().strip()
            width_str, height_str = raw.split(",")
            return int(width_str), int(height_str)
        except Exception as e:
            logger.error(f"Failed to read framebuffer size from {size_path}: {e}")
            raise

    def _read_fb_bpp(self):
        bpp_path = "/sys/class/graphics/fb0/bits_per_pixel"
        try:
            with open(bpp_path, "r") as f:
                return int(f.read().strip())
        except Exception as e:
            logger.error(f"Failed to read framebuffer bpp from {bpp_path}: {e}")
            raise

    def _read_fb_stride(self):
        stride_paths = [
            "/sys/class/graphics/fb0/stride",
            "/sys/class/graphics/fb0/line_length",
        ]
        for path in stride_paths:
            if os.path.exists(path):
                try:
                    with open(path, "r") as f:
                        return int(f.read().strip())
                except Exception as e:
                    logger.warning(f"Failed to read framebuffer stride from {path}: {e}")
        bytes_per_pixel = max(self.bits_per_pixel // 8, 1)
        return self.width * bytes_per_pixel

    def _open_fb(self):
        try:
            self.fb_fd = os.open(self.fb_path, os.O_RDWR)
            map_size = self.line_length * self.height
            self.fb_map = mmap.mmap(self.fb_fd, map_size, mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ)
        except Exception as e:
            logger.error(f"Failed to open framebuffer {self.fb_path}: {e}")
            raise

    def init_full_update(self):
        # No-op for framebuffer output.
        if self.enabled:
            logger.info("Framebuffer full update initialization complete.")

    def init_partial_update(self):
        # No-op for framebuffer output.
        if self.enabled:
            logger.info("Framebuffer partial update initialization complete.")

    def display_partial(self, image):
        if not self.enabled or self.fb_map is None:
            return
        try:
            frame = self._image_to_framebuffer(image)
            self.fb_map.seek(0)
            self.fb_map.write(frame)
            self.fb_map.flush()
            logger.info("Framebuffer update complete.")
        except Exception as e:
            logger.warning(f"Framebuffer write failed; disabling local display output: {e}")
            self.enabled = False

    def clear(self, color=(255, 255, 255)):
        if not self.enabled:
            return
        try:
            image = Image.new("RGB", (self.width, self.height), color)
            self.display_partial(image)
        except Exception as e:
            logger.warning(f"Error clearing framebuffer: {e}")

    def _image_to_framebuffer(self, image):
        rgb = self._prepare_frame_image(image)
        if self.bits_per_pixel == 16:
            raw = self._rgb_to_rgb565(rgb)
            return self._pad_lines(raw, 2)
        if self.bits_per_pixel == 32:
            raw = self._rgb_to_xrgb8888(rgb)
            return self._pad_lines(raw, 4)
        raise ValueError(f"Unsupported framebuffer format: {self.bits_per_pixel} bpp")

    def _prepare_frame_image(self, image):
        # Framebuffer is mounted with a 90-degree offset; rotate source clockwise.
        transformed = image.transpose(Image.ROTATE_270)
        src_w, src_h = transformed.size
        dst_w, dst_h = self.width, self.height

        if (src_w, src_h) != (dst_w, dst_h):
            # Preserve aspect ratio and fit inside framebuffer without cropping.
            scale = min(dst_w / src_w, dst_h / src_h)
            scaled_w = max(1, int(round(src_w * scale)))
            scaled_h = max(1, int(round(src_h * scale)))
            transformed = transformed.resize((scaled_w, scaled_h), Image.BILINEAR)
            canvas = Image.new("RGB", (dst_w, dst_h), (255, 255, 255))
            left = (dst_w - scaled_w) // 2
            top = (dst_h - scaled_h) // 2
            canvas.paste(transformed, (left, top))
            transformed = canvas

        return transformed.convert("RGB")

    def _rgb_to_rgb565(self, image):
        pixels = image.tobytes()
        out = bytearray(self.width * self.height * 2)
        out_idx = 0
        for i in range(0, len(pixels), 3):
            r = pixels[i]
            g = pixels[i + 1]
            b = pixels[i + 2]
            rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            out[out_idx] = rgb565 & 0xFF
            out[out_idx + 1] = (rgb565 >> 8) & 0xFF
            out_idx += 2
        return bytes(out)

    def _rgb_to_xrgb8888(self, image):
        pixels = image.tobytes()
        out = bytearray(self.width * self.height * 4)
        out_idx = 0
        for i in range(0, len(pixels), 3):
            r = pixels[i]
            g = pixels[i + 1]
            b = pixels[i + 2]
            out[out_idx] = b
            out[out_idx + 1] = g
            out[out_idx + 2] = r
            out[out_idx + 3] = 0x00
            out_idx += 4
        return bytes(out)

    def _pad_lines(self, raw, bytes_per_pixel):
        row_bytes = self.width * bytes_per_pixel
        if self.line_length == row_bytes:
            return raw
        if self.line_length < row_bytes:
            logger.warning("Framebuffer line_length is smaller than row size; writing without padding.")
            return raw
        out = bytearray(self.line_length * self.height)
        src_idx = 0
        dst_idx = 0
        for _ in range(self.height):
            out[dst_idx:dst_idx + row_bytes] = raw[src_idx:src_idx + row_bytes]
            src_idx += row_bytes
            dst_idx += self.line_length
        return bytes(out)

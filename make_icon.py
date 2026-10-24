# -*- coding: utf-8 -*-
"""生成应用图标 app.ico：黑底圆角 + 青色发光弧 + 指针"""
import math
from PIL import Image, ImageDraw, ImageFilter

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 背景圆角方块
d.rounded_rectangle([8, 8, S - 8, S - 8], radius=56, fill=(13, 17, 23, 255))

cx, cy, r = S / 2, S / 2 + 10, 82
start, span = 150, 240

# 底弧（暗）
bbox = [cx - r, cy - r, cx + r, cy + r]
d.arc(bbox, start=-(start + span), end=-start, fill=(255, 255, 255, 30), width=16)

# 亮弧（青色，75%）
glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
dg = ImageDraw.Draw(glow)
dg.arc(bbox, start=-(start + span * 0.75), end=-start, fill=(0, 242, 234, 255), width=14)
blur = glow.filter(ImageFilter.GaussianBlur(8))
img.alpha_composite(blur)
img.alpha_composite(glow)

# 指针尖端亮点
a = math.radians(start + span * 0.75)
tx, ty = cx + r * math.cos(a), cy - r * math.sin(a)
d.ellipse([tx - 12, ty - 12, tx + 12, ty + 12], fill=(0, 242, 234, 255))
d.ellipse([tx - 5, ty - 5, tx + 5, ty + 5], fill=(255, 255, 255, 255))

img.save("app.ico", sizes=[(256, 256), (48, 48), (32, 32), (16, 16)])
print("app.ico saved")

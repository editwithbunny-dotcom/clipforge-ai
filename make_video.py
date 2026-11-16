import subprocess, numpy as np
from PIL import Image
from skimage import data

def make(path, W=640, H=360, FPS=25, DUR=70):
    face = np.asarray(Image.fromarray(data.astronaut()[10:230, 110:310]).resize((130, 143)))
    p = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
                          "-f", "lavfi", "-i", f"sine=frequency=220:duration={DUR}", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], stdin=subprocess.PIPE)
    yy, xx = np.mgrid[0:H, 0:W]
    bg = np.stack([(40 + xx * 30 // W), (60 + yy * 40 // H), np.full_like(xx, 90)], -1).astype(np.uint8)
    for i in range(FPS * DUR):
        t = i / FPS
        x = int(60 + (W - 130 - 120) * (0.5 + 0.5 * np.sin(t / DUR * 2 * np.pi)))
        f = bg.copy(); f[100:243, x:x + 130] = face
        p.stdin.write(f.tobytes())
    p.stdin.close(); p.wait()

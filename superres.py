"""
Thermal integration and multi-frame super-resolution (numpy only).

integrate():   static camera -> temporal mean (noise drops ~ 1/sqrt(N)),
               optional drift registration.
superres():    hand-held (slightly moving) camera -> every frame samples the
               scene at a different sub-pixel offset. Frames are registered
               with sub-pixel phase correlation, fused onto a finer grid
               (shift-and-add) and refined by iterative back-projection
               (Irani & Peleg) using the Lepton's box pixel response.

Model: LR_k = Down_s( Shift(HR, s*D_k) ) + noise,
       where LR_k(p) ~= LR_ref(p + D_k) and Down_s is an s x s box mean.
"""
import numpy as np


# ---------------------------------------------------------------- helpers ---
def shift_bilinear(img, dy, dx):
    """out(y,x) = img(y+dy, x+dx), edge-clamped."""
    h, w = img.shape
    y = np.arange(h, dtype=np.float64) + dy
    x = np.arange(w, dtype=np.float64) + dx
    y0 = np.floor(y).astype(int)
    x0 = np.floor(x).astype(int)
    fy = (y - y0)[:, None]
    fx = (x - x0)[None, :]
    y0c, y1c = np.clip(y0, 0, h - 1), np.clip(y0 + 1, 0, h - 1)
    x0c, x1c = np.clip(x0, 0, w - 1), np.clip(x0 + 1, 0, w - 1)
    a = img[y0c]
    b = img[y1c]
    top = a[:, x0c] * (1 - fx) + a[:, x1c] * fx
    bot = b[:, x0c] * (1 - fx) + b[:, x1c] * fx
    return top * (1 - fy) + bot * fy


def shift_fast(img, dy, dx):
    """Same as shift_bilinear but via padded slicing (much faster)."""
    h, w = img.shape
    iy, ix = int(np.floor(dy)), int(np.floor(dx))
    fy, fx = dy - iy, dx - ix
    p = max(abs(iy), abs(ix)) + 2
    P = np.pad(img, p, mode="edge")
    def sl(oy, ox):
        y0 = p + iy + oy
        x0 = p + ix + ox
        return P[y0:y0 + h, x0:x0 + w]
    return ((1 - fy) * ((1 - fx) * sl(0, 0) + fx * sl(0, 1))
            + fy * ((1 - fx) * sl(1, 0) + fx * sl(1, 1)))


def blur_fast(img, sigma):
    if sigma <= 0:
        return img
    r = max(1, int(np.ceil(2.5 * sigma)))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    h, w = img.shape
    P = np.pad(img, ((r, r), (0, 0)), mode="edge")
    out = sum(k[i] * P[i:i + h] for i in range(2 * r + 1))
    P = np.pad(out, ((0, 0), (r, r)), mode="edge")
    return sum(k[i] * P[:, i:i + w] for i in range(2 * r + 1))


def upsample(lr, s):
    """Grid-aligned bilinear upsample: LR pixel i covers HR [i*s, (i+1)*s)."""
    h, w = lr.shape
    Y = (np.arange(h * s) - (s - 1) / 2.0) / s
    X = (np.arange(w * s) - (s - 1) / 2.0) / s
    y0 = np.floor(Y).astype(int)
    x0 = np.floor(X).astype(int)
    fy = (Y - y0)[:, None]
    fx = (X - x0)[None, :]
    y0c, y1c = np.clip(y0, 0, h - 1), np.clip(y0 + 1, 0, h - 1)
    x0c, x1c = np.clip(x0, 0, w - 1), np.clip(x0 + 1, 0, w - 1)
    a = lr[y0c]
    b = lr[y1c]
    top = a[:, x0c] * (1 - fx) + a[:, x1c] * fx
    bot = b[:, x0c] * (1 - fx) + b[:, x1c] * fx
    return top * (1 - fy) + bot * fy


def downsample(hr, s):
    H, W = hr.shape
    return hr.reshape(H // s, s, W // s, s).mean(axis=(1, 3))


def gaussian_blur(img, sigma):
    if sigma <= 0:
        return img
    r = int(np.ceil(3 * sigma))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    p = np.pad(img, r, mode="edge")
    p = np.apply_along_axis(lambda v: np.convolve(v, k, mode="valid"), 0, p)
    p = np.apply_along_axis(lambda v: np.convolve(v, k, mode="valid"), 1, p)
    return p


# ----------------------------------------------------------- registration ---
def _upsampled_dft(data, region, factor, offsets):
    im2pi = 2j * np.pi
    for n_items, ax_off in list(zip(data.shape, offsets))[::-1]:
        kernel = (np.arange(region) - ax_off)[:, None] * np.fft.fftfreq(n_items, factor)
        kernel = np.exp(-im2pi * kernel)
        data = np.tensordot(kernel, data, axes=(1, -1))
    return data


def _prep(img, window):
    hp = img - blur_fast(img, 6.0)       # remove large gradients / vignetting
    hp = hp - hp.mean()
    return hp * window


def register(ref_f, mov, window, upsample_factor=20):
    """Return (D, quality) with mov(p) ~= ref(p + D). ref_f = fft2 of prepared ref."""
    mov_f = np.fft.fft2(_prep(mov, window))
    prod = ref_f * mov_f.conj()
    # partial whitening: sharper than plain correlation, more noise-robust than pure phase
    prod = prod / np.maximum(np.abs(prod), 1e-12) ** 0.7
    cc = np.fft.ifft2(prod)
    shape = np.array(cc.shape)
    peak = np.array(np.unravel_index(np.argmax(np.abs(cc)), cc.shape), dtype=float)
    absc = np.abs(cc)
    quality = float(absc.max() / (np.median(absc) + 1e-12))
    mid = np.fix(shape / 2)
    peak[peak > mid] -= shape[peak > mid]
    shifts = np.round(peak * upsample_factor) / upsample_factor
    region = int(np.ceil(upsample_factor * 1.5))
    dftshift = np.fix(region / 2.0)
    offsets = dftshift - shifts * upsample_factor
    ucc = _upsampled_dft(prod.conj(), region, upsample_factor, offsets).conj()
    m = np.array(np.unravel_index(np.argmax(np.abs(ucc)), ucc.shape), dtype=float) - dftshift
    shifts = shifts + m / upsample_factor
    return shifts, quality


def register_all(frames, ref_idx=None, progress=None):
    n = len(frames)
    ref_idx = n // 2 if ref_idx is None else ref_idx
    h, w = frames[0].shape
    window = np.outer(np.hanning(h), np.hanning(w))
    ref_f = np.fft.fft2(_prep(frames[ref_idx], window))
    shifts = np.zeros((n, 2))
    qual = np.zeros(n)
    for i, f in enumerate(frames):
        if i == ref_idx:
            qual[i] = np.inf
        else:
            shifts[i], qual[i] = register(ref_f, f, window)
        if progress:
            progress(0.05 + 0.25 * (i + 1) / n, f"registering {i + 1}/{n}")
    return shifts, qual, ref_idx


def drop_frozen(frames, times=None):
    """Remove consecutive identical frames (Lepton freezes video during FFC)."""
    keep = [0]
    for i in range(1, len(frames)):
        if not np.array_equal(frames[i], frames[keep[-1]]):
            keep.append(i)
    return keep


# ------------------------------------------------------------ algorithms ---
def integrate(frames, align=False, progress=None):
    frames = [np.asarray(f, dtype=np.float64) for f in frames]
    keep = drop_frozen(frames)
    frames = [frames[i] for i in keep]
    shifts = np.zeros((len(frames), 2))
    if align and len(frames) > 2:
        shifts, _, _ = register_all(frames, progress=progress)
        frames = [shift_bilinear(f, -d[0], -d[1]) if np.any(d) else f for f, d in zip(frames, shifts)]
    stack = np.stack(frames)
    mean = stack.mean(axis=0)
    noise_single = float(np.median(stack.std(axis=0)))
    if progress:
        progress(1.0, "done")
    return {
        "image": mean,
        "baseline": frames[len(frames) // 2],
        "frames_used": len(frames),
        "frames_dropped": 0,
        "shifts": shifts.tolist(),
        "motion_px": float(np.sqrt(((shifts - shifts.mean(0)) ** 2).sum(1)).std()) if len(frames) > 1 else 0.0,
        "noise_single_K": noise_single,
        "noise_result_K": noise_single / np.sqrt(len(frames)),
        "scale": 1,
    }


def superres(frames, scale=3, iterations=30, step=1.75, progress=None,
             max_shift=6.0, min_quality=None):
    frames = [np.asarray(f, dtype=np.float64) for f in frames]
    keep = drop_frozen(frames)
    frames = [frames[i] for i in keep]
    n0 = len(frames)
    if n0 < 4:
        raise ValueError("need at least 4 distinct frames")
    if progress:
        progress(0.02, "registering frames")
    shifts, qual, ref_idx = register_all(frames, progress=progress)

    # Reject frames that registered badly (scene motion, FFC transitions, blur)
    finite_q = qual[np.isfinite(qual)]
    thr = min_quality if min_quality is not None else (np.median(finite_q) * 0.35 if len(finite_q) else 0)
    good = [i for i in range(n0)
            if (qual[i] >= thr) and np.all(np.abs(shifts[i]) <= max_shift)]
    frames = [frames[i] for i in good]
    shifts = shifts[good]
    n = len(frames)

    s = int(scale)
    D = shifts * s  # HR pixels

    # initial estimate: shift-and-add of upsampled frames
    X = np.zeros((frames[0].shape[0] * s, frames[0].shape[1] * s))
    ups = []
    for k, f in enumerate(frames):
        u = upsample(f, s)
        X += shift_fast(u, -D[k, 0], -D[k, 1])
    X /= n
    if progress:
        progress(0.35, "back-projecting")

    # noise estimate from residuals of the initial estimate
    resid = []
    for k in range(min(n, 8)):
        sim = downsample(shift_fast(X, D[k, 0], D[k, 1]), s)
        resid.append(frames[k] - sim)
    sigma = float(np.median(np.abs(np.stack(resid))) * 1.4826) + 1e-6
    clip = 4.0 * sigma

    # Iterative back-projection with Huber-clipped errors.
    # Back-projection kernel = pixel-replicate + small Gaussian (~bilinear).
    bp_sigma = 0.3 * s
    for it in range(iterations):
        G = np.zeros_like(X)
        err_rms = 0.0
        for k, f in enumerate(frames):
            sim = downsample(shift_fast(X, D[k, 0], D[k, 1]), s)
            e = np.clip(f - sim, -clip, clip)
            err_rms += float((e ** 2).mean())
            G += shift_fast(np.repeat(np.repeat(e, s, 0), s, 1), -D[k, 0], -D[k, 1])
        X += step * blur_fast(G, bp_sigma) / n
        if progress:
            progress(0.35 + 0.63 * (it + 1) / iterations,
                     f"iteration {it + 1}/{iterations}, rms err {np.sqrt(err_rms / n) * 1000:.0f} mK")

    centred = shifts - shifts.mean(0)
    motion = float(np.sqrt((centred ** 2).sum(1)).mean())
    ph = np.floor(np.mod(shifts, 1.0) * s).astype(int) % s
    subpixel_coverage = len(set(map(tuple, ph))) / float(s * s)   # 1.0 = every sub-pixel phase sampled

    if progress:
        progress(1.0, "done")
    return {
        "image": X,
        "baseline": upsample(frames[int(np.argmin(np.abs(shifts).sum(1)))], s),
        "frames_used": n,
        "frames_dropped": n0 - n + (len(keep) and 0),
        "frames_frozen": 0,
        "shifts": shifts.tolist(),
        "motion_px": motion,
        "subpixel_coverage": subpixel_coverage,
        "noise_single_K": sigma,
        "scale": s,
        "ref_index": int(ref_idx),
    }

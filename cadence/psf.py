"""Minimal psfascii reader for Spectre `-format psfascii` outputs.

Spectre writes one ASCII PSF file per analysis (e.g. `acPSRR.ac`, `nz.noise`,
`dcswp.dc`, `trn.tran.tran`). They share one VALUE-section grammar:

    VALUE
    "<sweep>" <x0>            # sweep var: "freq" (ac/noise) | "dc"/<param> | "time"
    "<sig>"   <v>            # real      scalar  (dc/tran nodes, noise "out")
    "<sig>"   (re im)        # complex   pair    (ac nodes)
    "<sig>"   ( a b c ... )  # group     array   (per-instance noise contributions)
    "<sweep>" <x1>           # ... next sweep point ...

`read_psf` returns {name: ndarray}: the sweep column + every signal column
(complex where Spectre gave a pair, float where scalar). Group-valued signals
(e.g. the per-instance noise breakdown) are returned as a list of arrays and
are simply ignored by callers that don't need them.
"""
import numpy as np


def read_psf(path):
    # Spectre's psfascii (`-format psfascii`, the dev fixture) is text; ADE/Maestro and
    # the cluster write BINARY PSF. Dispatch on the bytes so callers (importmp) read
    # either transparently -- the binary reader returns the identical dict shape and, in
    # fact, full double precision (psfascii rounds to ~6 sig figs). See cadence/binpsf.py.
    import binpsf
    if binpsf._is_binary(path):                       # single source of truth for the sniff
        return binpsf.read_binpsf(path)
    text = open(path).read()
    # isolate the VALUE..END body (section markers sit on their own lines)
    i = text.index("\nVALUE\n") + len("\nVALUE\n")
    body = text[i:]
    j = body.rfind("\nEND")
    if j != -1:
        body = body[:j]
    # make parens standalone tokens (Spectre attaches them to numbers: "(-1.2e-3")
    toks = body.replace("(", " ( ").replace(")", " ) ").split()

    cols = {}
    order = []
    sweep = None
    n = len(toks)
    k = 0
    while k < n:
        key = toks[k].strip('"')
        k += 1
        if k < n and toks[k] == "(":
            k += 1
            nums = []
            while k < n and toks[k] != ")":
                nums.append(float(toks[k]))
                k += 1
            k += 1  # consume ')'
            if len(nums) == 2:
                val = complex(nums[0], nums[1])
            elif len(nums) == 1:
                val = nums[0]
            else:
                val = np.asarray(nums)            # group (noise contributions)
        else:
            val = float(toks[k])
            k += 1
        if sweep is None:
            sweep = key
        if key not in cols:
            cols[key] = []
            order.append(key)
        cols[key].append(val)

    out = {}
    for name in order:
        v = cols[name]
        if any(isinstance(x, np.ndarray) for x in v):   # ragged group -> keep as list
            out[name] = v
        else:
            out[name] = np.asarray(v)
    out["_sweep"] = sweep
    return out


if __name__ == "__main__":
    import sys
    d = read_psf(sys.argv[1])
    print("sweep:", d["_sweep"])
    for kname, v in d.items():
        if kname == "_sweep":
            continue
        if isinstance(v, list):
            print(f"  {kname:12s} group[{len(v)}] x{len(v[0])}")
        else:
            print(f"  {kname:12s} {v.dtype} shape={v.shape}  "
                  f"[{v.flat[0]:.4g} ... {v.flat[-1]:.4g}]" if v.size else f"  {kname}: empty")

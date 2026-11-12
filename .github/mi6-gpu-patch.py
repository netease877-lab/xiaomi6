import pathlib, sys

def find(rel):
    for root in (pathlib.Path('.'), pathlib.Path('/home/pmos/build/src')):
        hits = sorted(root.rglob(rel))
        if hits:
            return hits[0]
    return None

p = find('adreno/a5xx_gpu.c')
if p is None:
    print('MI6PATCH: FATAL a5xx_gpu.c not found', flush=True)
    sys.exit(1)

s = p.read_text()
orig_len = len(s)

def sub(old, new, why):
    global s
    if old not in s:
        print('MI6PATCH: FATAL anchor missing (%s): %r' % (why, old[:70]), flush=True)
        sys.exit(1)
    if s.count(old) != 1:
        print('MI6PATCH: FATAL anchor ambiguous (%s) count=%d' % (why, s.count(old)), flush=True)
        sys.exit(1)
    s = s.replace(old, new, 1)

# A) includes + module params，默认全部回退到 upstream 语义
sub('#include "a5xx_gpu.h"',
'''#include "a5xx_gpu.h"
#include <linux/moduleparam.h>

/* ==== mi6: GPU 回退/诊断开关（默认走 upstream 语义）==== */
static int a5xx_pp_as = 0;
module_param_named(a5xx_pp_as, a5xx_pp_as, int, 0600);
MODULE_PARM_DESC(a5xx_pp_as, "mi6: 1=per-process GPU address space (fork), 0=shared (upstream)");

static int a5xx_nr_rings = 1;
module_param_named(a5xx_nr_rings, a5xx_nr_rings, int, 0600);
MODULE_PARM_DESC(a5xx_nr_rings, "mi6: forced a5xx ring count, 1 disables preemption");

static int a5xx_whereami = 0;
module_param_named(a5xx_whereami, a5xx_whereami, int, 0600);
MODULE_PARM_DESC(a5xx_whereami, "mi6: -1=auto, 0=force no WHERE_AM_I, 1=force yes");''', 'params')

# B) 关闭 fork 的 per-process 页表（返回 NULL -> msm_gpu 回落共享 aspace）
sub('\tmmu = msm_iommu_pagetable_create(gpu->aspace->mmu);',
'''\tif (!a5xx_pp_as) {
\t\tpr_info("msm: a5xx private address space disabled (upstream shared aspace)\\n");
\t\treturn NULL;
\t}
\tmmu = msm_iommu_pagetable_create(gpu->aspace->mmu);''', 'pp_as')

# C) ring 数可调，默认 1（关 preemption，绕过 a5xx_active_ring 只看 cur_ring 的盲区）
sub('ret = adreno_gpu_init(dev, pdev, adreno_gpu, &funcs, 4);',
'''ret = adreno_gpu_init(dev, pdev, adreno_gpu, &funcs,
\t\t\ta5xx_nr_rings > 0 ? a5xx_nr_rings : 4);''', 'nr_rings')

# D) hw_init 结束后打印实际生效参数
sub('\ta5xx_preempt_hw_init(gpu);',
'''\ta5xx_preempt_hw_init(gpu);

\tpr_info("msm: a5xx hw_init: nr_rings=%d has_whereami=%d pp_as=%d\\n",
\t\tgpu->nr_rings, a5xx_gpu->has_whereami, a5xx_pp_as);''', 'hw_init_dbg')

# E) 微码探测结果 + 强制覆盖 WHERE_AM_I
sub('''\tif (((buf[0] & 0xf) == 0xa) && (buf[2] & 0xf) >= 1)
\t\ta5xx_gpu->has_whereami = true;''',
'''\tif (((buf[0] & 0xf) == 0xa) && (buf[2] & 0xf) >= 1)
\t\ta5xx_gpu->has_whereami = true;

\tpr_info("msm: a5xx ucode hdr %08x %08x %08x -> detected whereami=%d\\n",
\t\tbuf[0], buf[1], buf[2], a5xx_gpu->has_whereami);

\tif (a5xx_whereami >= 0) {
\t\ta5xx_gpu->has_whereami = !!a5xx_whereami;
\t\tpr_info("msm: a5xx whereami forced to %d\\n", a5xx_gpu->has_whereami);
\t}''', 'ucode_dbg')

# F) rptr 来源对比（shadow 内存 vs 真实 CP_RB_RPTR 寄存器）
sub('''\tif (a5xx_gpu->has_whereami)
\t\treturn a5xx_gpu->shadow[ring->id];''',
'''\tif (a5xx_gpu->has_whereami) {
\t\tpr_info_ratelimited("msm: rptr ring%d shadow=%u hw=%u wptr=%u\\n",
\t\t\tring->id, a5xx_gpu->shadow[ring->id],
\t\t\tgpu_read(gpu, REG_A5XX_CP_RB_RPTR), ring->wptr);
\t\treturn a5xx_gpu->shadow[ring->id];
\t}''', 'rptr_dbg')

p.write_text(s)
print('MI6PATCH: ok %s (%d -> %d bytes)' % (p, orig_len, len(s)), flush=True)

# G) 单 ring 时把高优先级 submitqueue 并到 ring 0，避免 Mesa 建队列被 EINVAL 拒掉
h = find('msm/msm_gpu.h')
if h is None:
    print('MI6PATCH: FATAL msm_gpu.h not found', flush=True)
    sys.exit(1)
t = h.read_text()
old_g = '''\tif (rn >= gpu->nr_rings)
\t\treturn -EINVAL;'''
new_g = '''\tif (rn >= gpu->nr_rings) {
\t\t/*
\t\t * mi6: 单 ring 时把更高优先级的 submitqueue 并到 ring 0。
\t\t * Mesa 会按 rn=2 建队列，若仍返回 -EINVAL，nr_rings=1 下就开不了设备。
\t\t */
\t\tif (gpu->nr_rings == 1)
\t\t\trn = 0;
\t\telse
\t\t\treturn -EINVAL;
\t}'''
if old_g not in t or t.count(old_g) != 1:
    print('MI6PATCH: FATAL msm_gpu.h anchor missing/ambiguous', flush=True)
    sys.exit(1)
h.write_text(t.replace(old_g, new_g, 1))
print('MI6PATCH: ok single-ring priority clamp %s' % h, flush=True)

# H) hang_debug 默认打开（定义位置随版本变动，找不到只告警）
q = find('msm/msm_gpu.c')
if q is not None and 'bool hang_debug = false;' in q.read_text():
    u = q.read_text().replace('bool hang_debug = false;', 'bool hang_debug = true;', 1)
    q.write_text(u)
    print('MI6PATCH: ok hang_debug=true %s' % q, flush=True)
else:
    print('MI6PATCH: WARN hang_debug 默认值未找到，跳过（不影响主修复）', flush=True)

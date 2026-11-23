import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
PLATFORM = TOOLS / "xbox" / "platform"

spec = importlib.util.spec_from_file_location("xbox_dxil_lower_address", PLATFORM / "xbox_dxil_lower.py")
lower_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = lower_module
spec.loader.exec_module(lower_module)


def function(body):
    return "\n".join([
        'target triple = "dxil-ms-xb2"',
        '%dx.types.Handle = type { i8* }',
        '',
        'define void @main() {',
        *body,
        '  ret void',
        '}',
        '',
        '!5 = !{void ()* @main, !"main", null, null, !6}',
        '!6 = !{i32 0, i64 1048576, i32 4, !7}',
        '!7 = !{i32 64, i32 1, i32 1}',
        '',
    ])


class XboxDxilAddressLowerTests(unittest.TestCase):
    def test_kernel_has_no_xbox_calls_and_sets_heap_flag(self):
        text = (PLATFORM / "address_kernel.ll").read_text(encoding="utf-8")
        result, count, left = lower_module.lower(text)
        self.assertEqual(left, set())
        self.assertEqual(count, 13)
        self.assertNotIn("@xbox.", result)
        flags = int(re.search(r"!\{i32 0, i64 (\d+), i32 4", result).group(1))
        self.assertTrue(flags & (1 << 30))
        for name in ("createHandleFromHeap", "annotateHandle", "rawBufferLoad.i32", "rawBufferStore.i32", "atomicBinOp.i32"):
            self.assertRegex(result, r"(?m)^declare [^\n]*@dx\.op\.%s\(" % re.escape(name))
        self.assertIn("%dx.types.ResourceProperties = type { i32, i32 }", result)
        self.assertIn("%dx.types.ResRet.i32 = type", result)

    def test_load_searches_the_table_then_reads_through_the_entry_heap_index(self):
        result, _, left = lower_module.lower(function([
            '  %1 = call { i32, i32, i32 } @xbox.global_load_dwordx3_with_cache(i32 358, i64 4096, i32 undef, i32 undef)',
            '  %2 = extractvalue { i32, i32, i32 } %1, 2',
        ]))
        self.assertEqual(left, set())
        table_loads = re.findall(r"rawBufferLoad\.i32\(i32 139, %dx\.types\.Handle %\S+, i32 %\S+, i32 undef, i8 (\d+), i32 4\)", result)
        self.assertEqual(table_loads.count("3"), lower_module.ADDRESS_TABLE_LOG2)
        self.assertIn("7", table_loads)
        self.assertEqual(len(re.findall(r"createHandleFromHeap\(i32 218, i32 0, i1 false, i1 false\)", result)), 1)
        self.assertEqual(len(re.findall(r"createHandleFromHeap\(i32 218, i32 %\S+, i1 false, i1 true\)", result)), 1)
        self.assertNotRegex(result, r"\{ i32, i32, i32 \}")

    def test_store_of_a_literal_based_aggregate(self):
        result, _, left = lower_module.lower(function([
            '  %1 = insertvalue { i32, i32, i32, i32 } { i32 -8388609, i32 -8388609, i32 0, i32 undef }, i32 7, 3',
            '  call void @xbox.global_store_dwordx4_with_cache(i32 363, i64 8192, { i32, i32, i32, i32 } %1, i32 undef, i32 undef)',
        ]))
        self.assertEqual(left, set())
        self.assertRegex(result, r"rawBufferStore\.i32\(i32 140, %dx\.types\.Handle %\S+, i32 %\S+, i32 undef, i32 -8388609, i32 -8388609, i32 0, i32 7, i8 15, i32 4\)")

    def test_float_min_is_two_integer_atomics(self):
        result, _, left = lower_module.lower(function([
            '  %1 = call float @xbox.global_atomic_fmin(i32 366, i64 64, float 1.000000e+00)',
        ]))
        self.assertEqual(left, set())
        ops = re.findall(r"atomicBinOp\.i32\(i32 78, %dx\.types\.Handle %\S+, i32 (\d+),", result)
        self.assertEqual(ops, ["4", "7"])

    def test_copysign_becomes_bit_operations(self):
        result, count, _ = lower_module.lower(function([
            '  %1 = call float @llvm.copysign.f32(float 1.000000e+00, float -2.000000e+00)',
        ]) + "declare float @llvm.copysign.f32(float, float) #0\n")
        self.assertEqual(count, 1)
        self.assertNotIn("llvm.copysign", result)
        self.assertIn("and i32", result)

    def test_store_dwordx2_lowers_cleanly(self):
        result, _, left = lower_module.lower(function([
            '  %1 = insertvalue { i32, i32 } undef, i32 10, 0',
            '  %2 = insertvalue { i32, i32 } %1, i32 20, 1',
            '  call void @xbox.global_store_dwordx2_with_cache(i32 363, i64 8192, { i32, i32 } %2, i32 undef, i32 undef)',
        ]))
        self.assertEqual(left, set())
        self.assertRegex(result, r"rawBufferStore\.i32\(i32 140, %dx\.types\.Handle %\S+, i32 %\S+, i32 undef, i32 10, i32 20, i32 undef, i32 undef, i8 3, i32 4\)")

    def test_integer_atomics(self):
        result, _, left = lower_module.lower(function([
            '  %1 = call i32 @xbox.global_atomic_and(i32 370, i64 64, i32 255)',
            '  %2 = call i32 @xbox.global_atomic_or(i32 371, i64 64, i32 1)',
            '  %3 = call i32 @xbox.global_atomic_xor(i32 372, i64 64, i32 2)',
            '  %4 = call i32 @xbox.global_atomic_smin(i32 373, i64 64, i32 -5)',
            '  %5 = call i32 @xbox.global_atomic_smax(i32 374, i64 64, i32 5)',
            '  %6 = call i32 @xbox.global_atomic_umin(i32 375, i64 64, i32 10)',
            '  %7 = call i32 @xbox.global_atomic_umax(i32 376, i64 64, i32 20)',
            '  %8 = call i32 @xbox.global_atomic_swap(i32 377, i64 64, i32 30)',
        ]))
        self.assertEqual(left, set())
        ops = re.findall(r"atomicBinOp\.i32\(i32 78, %dx\.types\.Handle %\S+, i32 (\d+),", result)
        self.assertEqual(ops, ["1", "2", "3", "4", "5", "6", "7", "8"])

    def test_typed_load_kernel_lowers_cleanly(self):
        text = (PLATFORM / "typedload_kernel.ll").read_text(encoding="utf-8")
        result, count, left = lower_module.lower(text)
        self.assertEqual(left, set())
        self.assertEqual(count, 2)
        self.assertNotIn("@xbox.", result)
        self.assertIn("rawBufferLoad.i32", result)


class XboxDxilSm66ResourceTests(unittest.TestCase):
    def rewrite(self, text, wave="64"):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "in.ll", Path(directory) / "out.ll"
            source.write_text(text, encoding="utf-8")
            subprocess.run([sys.executable, "-I", str(PLATFORM / "dxil_sm66_wave.py"), str(source), str(target), wave], check=True, capture_output=True)
            return target.read_text(encoding="utf-8")

    def resources(self):
        return "\n".join([
            'target triple = "dxil-ms-dx"',
            '%dx.types.Handle = type { i8* }',
            '',
            'define void @main() {',
            '  %1 = call %dx.types.Handle @dx.op.createHandle(i32 57, i8 0, i32 0, i32 0, i1 false)',
            '  %2 = call %dx.types.Handle @dx.op.createHandle(i32 57, i8 1, i32 1, i32 3, i1 false)',
            '  ret void',
            '}',
            '',
            'declare %dx.types.Handle @dx.op.createHandle(i32, i8, i32, i32, i1) #0',
            '!dx.resources = !{!8}',
            '!1 = !{i32 1, i32 5}',
            '!2 = !{!"cs", i32 6, i32 5}',
            '!3 = !{i32 0, i64 0, i32 4, !2}',
            '!8 = !{!9, !11, null, null}',
            '!9 = !{!10}',
            '!10 = !{i32 0, %"class.StructuredBuffer<vector<unsigned int, 2> >"* undef, !"", i32 0, i32 0, i32 1, i32 12, i32 0, !13}',
            '!11 = !{!12}',
            '!12 = !{i32 1, %"class.RWStructuredBuffer<unsigned int>"* undef, !"", i32 2, i32 3, i32 1, i32 12, i1 true, i1 false, i1 false, !14}',
            '!13 = !{i32 1, i32 8}',
            '!14 = !{i32 1, i32 4}',
            '',
        ])

    def test_structured_buffers_keep_kind_stride_and_binding(self):
        out = self.rewrite(self.resources())
        self.assertIn("%dx.types.ResBind { i32 0, i32 0, i32 0, i8 0 }", out)
        self.assertIn("%dx.types.ResourceProperties { i32 12, i32 8 }", out)
        self.assertIn("%dx.types.ResBind { i32 3, i32 3, i32 2, i8 1 }", out)
        self.assertIn("%dx.types.ResourceProperties { i32 20492, i32 4 }", out)

    def test_wave_zero_leaves_the_wave_size_unpinned(self):
        self.assertNotIn("i32 11, !9999", self.rewrite(self.resources(), "0"))
        self.assertIn("i32 11, !9999", self.rewrite(self.resources(), "64"))

    def test_named_handle_is_converted(self):
        out = self.rewrite(self.resources().replace("%1 = call", "%cb = call"), "0")
        self.assertNotIn("@dx.op.createHandle(i32 57", out)
        self.assertIn("%hraw.cb = call %dx.types.Handle @dx.op.createHandleFromBinding(", out)
        self.assertIn("%cb = call %dx.types.Handle @dx.op.annotateHandle(i32 216, %dx.types.Handle %hraw.cb,", out)


class ProxyAddressKernelTests(unittest.TestCase):
    def test_proxy_kernel_reads_both_bases_from_the_constant_buffer(self):
        text = (PLATFORM / "proxy_address_kernel.ll").read_text(encoding="utf-8")
        result, count, left = lower_module.lower(text)
        self.assertEqual(left, set())
        self.assertEqual(count, 13)
        self.assertNotRegex(result, r"i64 (20015998304256|8589930496|8590979072|8590979200)")
        self.assertIn("@dx.op.cbufferLoadLegacy.i32(i32 59", result)
        self.assertRegex(result, r"(?m)^!\d+ = !\{null, null, !\d+, null\}$")
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "in.ll", Path(directory) / "out.ll"
            source.write_text(result, encoding="utf-8")
            subprocess.run([sys.executable, "-I", str(PLATFORM / "dxil_sm66_wave.py"), str(source), str(target), "0"], check=True, capture_output=True)
            out = target.read_text(encoding="utf-8")
        self.assertNotIn("@dx.op.createHandle(i32 57", out)
        self.assertIn("i8 2 }", out)
        self.assertNotIn("i32 11, !9999", out)


if __name__ == "__main__":
    unittest.main()

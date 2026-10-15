#!/usr/bin/env python3
"""Compile exact upstream list_parts body with a deterministic bucket-list fake.

This is a focused source regression, not a replacement for an S3 integration
test or a compiled production Ceph binary. Requires a C++17 compiler.
"""
import pathlib
import subprocess
import sys
import tempfile

source = pathlib.Path(sys.argv[1]).read_text()
start = source.index('int POSIXMultipartUpload::list_parts(')
end = source.index('\nint POSIXMultipartUpload::abort(', start)
body = source[start:end]
prefix = r'''
#include <algorithm>
#include <cstdio>
#include <iostream>
#include <map>
#include <memory>
#include <string>
#include <vector>
using ulong = unsigned long;
struct DoutPrefixProvider {};
struct CephContext {};
using optional_yield = int;
namespace fmt {
std::string format(const char*, int n) {
  char s[32]; std::snprintf(s, sizeof(s), "%05d", n); return s;
}}
const std::string MP_OBJ_PART_PFX = "part.";
struct rgw_obj_key : std::string {
  using std::string::string;
  using std::string::operator=;
  std::string ns;
  rgw_obj_key(const std::string& s) : std::string(s) {}
  rgw_obj_key() = default;
};
struct rgw_bucket_dir_entry { std::string key; };
namespace rgw::sal {
struct Bucket {
  struct ListParams { std::string prefix, ns; rgw_obj_key marker; };
  struct ListResults { std::vector<rgw_bucket_dir_entry> objs; bool is_truncated = false; };
  int count;
  explicit Bucket(int n) : count(n) {}
  int list(const DoutPrefixProvider*, ListParams& p, int max, ListResults& r, int) {
    int marker = std::stoi(p.marker.substr(5));
    for (int n = marker+1; n <= count && (int)r.objs.size() < max; ++n)
      r.objs.push_back({"part." + fmt::format("", n)});
    // POSIXBucket::list sets truncation at count >= max (not > max).
    r.is_truncated = count - marker >= max;
    return 0;
  }
};
}
struct MultipartPart { virtual ~MultipartPart() = default; virtual int get_num() const = 0; };
struct POSIXMultipartUpload;
struct POSIXMultipartPart : MultipartPart {
  int n = 0;
  explicit POSIXMultipartPart(POSIXMultipartUpload*) {}
  int load(const DoutPrefixProvider*, int, void*, rgw_obj_key& k) { n = std::stoi(k.substr(5)); return 0; }
  int get_num() const override { return n; }
};
struct POSIXMultipartUpload {
  std::map<int, std::unique_ptr<MultipartPart>> parts;
  rgw::sal::Bucket* shadow;
  std::string mp_ns = "multipart";
  void* driver = nullptr;
  int load(const DoutPrefixProvider*) { return 0; }
  int list_parts(const DoutPrefixProvider*, CephContext*, int, int, int*, bool*, optional_yield, bool = false);
};
'''
suffix = r'''
int main() {
  bool failed = false;
  for (int count : {1, 999, 1000, 1001, 1002, 2000, 2001, 10000}) {
    rgw::sal::Bucket bucket(count);
    POSIXMultipartUpload upload; upload.shadow = &bucket;
    int marker = 0, expected = 1, total = 0;
    bool truncated = false, ok = true;
    do {
      upload.list_parts(nullptr, nullptr, 1000, marker, &marker, &truncated, 0);
      total += upload.parts.size();
      for (auto& [n, part] : upload.parts) {
        if (n != expected++) { ok = false; break; }
      }
      if (!ok || total > count) break;
    } while (truncated);
    ok = ok && total == count && expected == count+1;
    std::cout << "parts=" << count << " " << (ok ? "PASS" : "FAIL") << " listed=" << total << '\n';
    failed |= !ok;
  }
  return failed ? 1 : 0;
}
'''
with tempfile.TemporaryDirectory() as temp:
    test = pathlib.Path(temp) / 'pagination.cc'
    binary = pathlib.Path(temp) / 'pagination'
    test.write_text(prefix + body + suffix)
    subprocess.run(['c++', '-std=c++17', '-O0', str(test), '-o', str(binary)], check=True)
    raise SystemExit(subprocess.run([str(binary)]).returncode)

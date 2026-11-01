import urllib.request
import json

def post_convert(filename, content, target_format):
    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="target_format"\r\n\r\n'
        f"{target_format}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
        f"Content-Type: text/plain\r\n\r\n"
        f"{content}\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://universal-file-converter-xmxl.onrender.com/api/convert",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    res = urllib.request.urlopen(req)
    return json.loads(res.read())

print("=== VERIFYING LIVE CLOUD CONVERSIONS ===")

# Test 1: text -> docx
res_docx = post_convert("live_test.txt", "Hello Docx World from Live Render Test", "docx")
print("DOCX response:", res_docx)
hosted_docx = res_docx["results"][0]["hosted_name"]

# Download docx
d_res = urllib.request.urlopen(f"https://universal-file-converter-xmxl.onrender.com/files/{hosted_docx}")
d_bytes = d_res.read()
print(f"Downloaded docx: {len(d_bytes)} bytes, Magic: {d_bytes[:4]}")
assert d_bytes[:4] == b"PK\x03\x04", "Expected ZIP/DOCX magic header"

# Cleanup docx
req_del = urllib.request.Request(
    f"https://universal-file-converter-xmxl.onrender.com/api/files/{hosted_docx}",
    method="DELETE"
)
print("Delete docx:", urllib.request.urlopen(req_del).read().decode())

# Test 2: text -> rtf
res_rtf = post_convert("live_test_rtf.txt", "Hello RTF World with Special Characters", "rtf")
print("RTF response:", res_rtf)
hosted_rtf = res_rtf["results"][0]["hosted_name"]

# Download rtf
d_rtf_res = urllib.request.urlopen(f"https://universal-file-converter-xmxl.onrender.com/files/{hosted_rtf}")
d_rtf_bytes = d_rtf_res.read()
print(f"Downloaded rtf: {len(d_rtf_bytes)} bytes, Header: {d_rtf_bytes[:6]}")
assert d_rtf_bytes.startswith(b"{\\rtf1"), "Expected RTF header"

# Cleanup rtf
req_del_rtf = urllib.request.Request(
    f"https://universal-file-converter-xmxl.onrender.com/api/files/{hosted_rtf}",
    method="DELETE"
)
print("Delete rtf:", urllib.request.urlopen(req_del_rtf).read().decode())
print("ALL LIVE CLOUD DOCUMENT CONVERSIONS VERIFIED SUCCESSFULLY!")

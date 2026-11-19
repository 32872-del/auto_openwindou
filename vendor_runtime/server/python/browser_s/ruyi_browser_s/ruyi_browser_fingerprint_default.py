"""ruyi 默认指纹参数。

这些内容来自 ``ruyipage/指纹参数/profile1.txt`` 到 ``profile20.txt``。
服务端首次运行时把它们释放到程序同级的 ``如意指纹参数`` 文件夹，
后续用户可以直接增删或替换该文件夹中的 ``.txt`` 文件。
"""

from __future__ import annotations


DEFAULT_RUYI_FINGERPRINTS: dict[str, str] = {
    "profile1.txt": """webdriver:0
local_webrtc:192.168.0.2
public_webrtc:104.18.22.67
timezone:America/New_York
font_system:windows
useragent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36
hardwareConcurrency:8
webglrenderer:Google Inc. (Google)
webglvendor:ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)
width:1920
height:1080
canvas:13572
""",
    "profile2.txt": """webdriver:0
local_webrtc:10.0.0.45
public_webrtc:185.199.108.153
timezone:America/Los_Angeles
font_system:windows
useragent:Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0
hardwareConcurrency:4
webglrenderer:NVIDIA Corporation (NVIDIA)
webglvendor:ANGLE (NVIDIA, NVIDIA GeForce RTX 2070 Direct3D11 vs_5_0 ps_5_0, D3D11)
width:1366
height:768
canvas:27841
""",
    "profile3.txt": """webdriver:0
local_webrtc:172.16.1.33
public_webrtc:91.108.56.180
timezone:Europe/London
font_system:windows
useragent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0
hardwareConcurrency:6
webglrenderer:Google Inc. (Google)
webglvendor:ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)
width:1600
height:900
canvas:54319
""",
    "profile4.txt": """webdriver:0
local_webrtc:192.168.4.12
public_webrtc:23.227.38.65
timezone:Europe/Berlin
font_system:windows
useragent:Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0
hardwareConcurrency:8
webglrenderer:NVIDIA Corporation (NVIDIA)
webglvendor:ANGLE (NVIDIA, NVIDIA GeForce GTX 1080 Direct3D11 vs_5_0 ps_5_0, D3D11)
width:2560
height:1440
canvas:38291
""",
    "profile5.txt": """webdriver:0
local_webrtc:10.10.0.77
public_webrtc:67.205.160.22
timezone:Asia/Tokyo
font_system:windows
useragent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36
hardwareConcurrency:16
webglrenderer:Google Inc. (Google)
webglvendor:ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0, D3D11)
width:1920
height:1200
canvas:61047
""",
    "profile6.txt": """webdriver:0
local_webrtc:192.168.2.88
public_webrtc:138.68.14.55
timezone:America/Chicago
font_system:windows
useragent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36 Edg/118.0.0.0
hardwareConcurrency:4
webglrenderer:Google Inc. (Google)
webglvendor:ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)
width:1280
height:800
canvas:19384
""",
    "profile7.txt": """webdriver:0
local_webrtc:10.0.1.55
public_webrtc:159.89.214.31
timezone:America/Toronto
font_system:windows
useragent:Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:119.0) Gecko/20100101 Firefox/119.0
hardwareConcurrency:8
webglrenderer:NVIDIA Corporation (NVIDIA)
webglvendor:ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Super Direct3D11 vs_5_0 ps_5_0, D3D11)
width:1440
height:900
canvas:72635
""",
    "profile8.txt": """webdriver:0
local_webrtc:172.20.1.9
public_webrtc:45.55.44.100
timezone:Europe/Paris
font_system:windows
useragent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36
hardwareConcurrency:12
webglrenderer:Google Inc. (Google)
webglvendor:ANGLE (AMD, AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0, D3D11)
width:1920
height:1080
canvas:83417
""",
    "profile9.txt": """webdriver:0
local_webrtc:192.168.6.44
public_webrtc:212.71.255.20
timezone:Asia/Singapore
font_system:windows
useragent:Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0
hardwareConcurrency:8
webglrenderer:NVIDIA Corporation (NVIDIA)
webglvendor:ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)
width:1920
height:1080
canvas:47829
""",
    "profile10.txt": """webdriver:0
local_webrtc:10.0.3.66
public_webrtc:198.199.119.44
timezone:Australia/Sydney
font_system:windows
useragent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0
hardwareConcurrency:4
webglrenderer:Google Inc. (Google)
webglvendor:ANGLE (AMD, AMD Radeon RX 5600 XT Direct3D11 vs_5_0 ps_5_0, D3D11)
width:1280
height:1024
canvas:31056
""",
    "profile11.txt": """webdriver:0
local_webrtc:172.16.2.18
public_webrtc:104.21.88.43
timezone:Europe/Amsterdam
font_system:windows
useragent:Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:118.0) Gecko/20100101 Firefox/118.0
hardwareConcurrency:6
webglrenderer:NVIDIA Corporation (NVIDIA)
webglvendor:ANGLE (NVIDIA, NVIDIA GeForce GTX 970 Direct3D11 vs_5_0 ps_5_0, D3D11)
width:1680
height:1050
canvas:59203
""",
    "profile12.txt": """webdriver:0
local_webrtc:192.168.7.99
public_webrtc:67.207.67.3
timezone:America/Denver
font_system:windows
useragent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36
hardwareConcurrency:8
webglrenderer:Google Inc. (Google)
webglvendor:ANGLE (NVIDIA, NVIDIA GeForce GTX 1070 Direct3D11 vs_5_0 ps_5_0, D3D11)
width:1920
height:1080
canvas:22947
""",
    "profile13.txt": """webdriver:0
local_webrtc:10.0.4.21
public_webrtc:185.220.100.77
timezone:Europe/Madrid
font_system:windows
useragent:Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0
hardwareConcurrency:4
webglrenderer:NVIDIA Corporation (NVIDIA)
webglvendor:ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 Direct3D11 vs_5_0 ps_5_0, D3D11)
width:1366
height:768
canvas:66712
""",
    "profile14.txt": """webdriver:0
local_webrtc:172.16.3.55
public_webrtc:138.197.62.11
timezone:Asia/Seoul
font_system:windows
useragent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36
hardwareConcurrency:16
webglrenderer:Google Inc. (Google)
webglvendor:ANGLE (AMD, AMD Radeon RX 6800 XT Direct3D11 vs_5_0 ps_5_0, D3D11)
width:2560
height:1440
canvas:91034
""",
    "profile15.txt": """webdriver:0
local_webrtc:192.168.8.14
public_webrtc:45.76.100.55
timezone:Asia/Shanghai
font_system:windows
useragent:Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0
hardwareConcurrency:8
webglrenderer:NVIDIA Corporation (NVIDIA)
webglvendor:ANGLE (NVIDIA, NVIDIA GeForce RTX 2080 Direct3D11 vs_5_0 ps_5_0, D3D11)
width:1920
height:1080
canvas:44561
""",
    "profile16.txt": """webdriver:0
local_webrtc:10.0.5.38
public_webrtc:159.65.200.88
timezone:Europe/Rome
font_system:windows
useragent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0
hardwareConcurrency:4
webglrenderer:Google Inc. (Google)
webglvendor:ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)
width:1280
height:800
canvas:78234
""",
    "profile17.txt": """webdriver:0
local_webrtc:172.16.4.77
public_webrtc:212.71.250.33
timezone:America/Phoenix
font_system:windows
useragent:Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:119.0) Gecko/20100101 Firefox/119.0
hardwareConcurrency:8
webglrenderer:NVIDIA Corporation (NVIDIA)
webglvendor:ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)
width:1600
height:900
canvas:35678
""",
    "profile18.txt": """webdriver:0
local_webrtc:192.168.9.52
public_webrtc:104.21.44.99
timezone:Europe/Stockholm
font_system:windows
useragent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36
hardwareConcurrency:12
webglrenderer:Google Inc. (Google)
webglvendor:ANGLE (AMD, AMD Radeon RX 5700 Direct3D11 vs_5_0 ps_5_0, D3D11)
width:1920
height:1080
canvas:52891
""",
    "profile19.txt": """webdriver:0
local_webrtc:10.0.6.14
public_webrtc:198.199.77.22
timezone:Asia/Kolkata
font_system:windows
useragent:Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0
hardwareConcurrency:4
webglrenderer:NVIDIA Corporation (NVIDIA)
webglvendor:ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)
width:1440
height:900
canvas:17463
""",
    "profile20.txt": """webdriver:0
local_webrtc:172.16.5.88
public_webrtc:67.205.180.44
timezone:America/Sao_Paulo
font_system:windows
useragent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0
hardwareConcurrency:8
webglrenderer:Google Inc. (Google)
webglvendor:ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)
width:1920
height:1080
canvas:88127
""",
}


__all__ = ["DEFAULT_RUYI_FINGERPRINTS"]

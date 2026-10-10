"""Read-only hub descriptor queries for the selected C077 device."""
import ctypes as c
from ctypes import wintypes as w
import json
from pathlib import Path
import struct
import uuid

k = c.WinDLL('kernel32', use_last_error=True)
cm = c.WinDLL('cfgmgr32')
k.CreateFileW.argtypes = [w.LPCWSTR,w.DWORD,w.DWORD,c.c_void_p,w.DWORD,w.DWORD,w.HANDLE]
k.CreateFileW.restype = w.HANDLE
k.DeviceIoControl.argtypes = [w.HANDLE,w.DWORD,c.c_void_p,w.DWORD,c.c_void_p,w.DWORD,c.POINTER(w.DWORD),c.c_void_p]
k.DeviceIoControl.restype = w.BOOL
k.CloseHandle.argtypes = [w.HANDLE]
guid = (c.c_byte * 16).from_buffer_copy(uuid.UUID('f18a0e88-c30c-11d0-8815-00a0c906bed8').bytes_le)
cm.CM_Get_Device_Interface_List_SizeW.argtypes = [c.POINTER(w.ULONG), c.c_void_p,w.LPCWSTR,w.ULONG]
cm.CM_Get_Device_Interface_ListW.argtypes = [c.c_void_p,w.LPCWSTR,w.LPWSTR,w.ULONG,w.ULONG]
parent = 'USB\\ROOT_HUB30\\4&2fd48294&0&0'
size=w.ULONG()
assert cm.CM_Get_Device_Interface_List_SizeW(c.byref(size),guid,parent,0)==0
paths=c.create_unicode_buffer(size.value)
assert cm.CM_Get_Device_Interface_ListW(guid,parent,paths,size,0)==0
path=paths.value
handle=k.CreateFileW(path,0x40000000,3,None,3,0,None)
if handle == c.c_void_p(-1).value:
    raise c.WinError(c.get_last_error())

def query(code, payload, length=4096):
    buf=c.create_string_buffer(length)
    c.memmove(buf,payload,len(payload))
    used=w.DWORD()
    if not k.DeviceIoControl(handle,code,buf,length,buf,length,c.byref(used),None):
        raise c.WinError(c.get_last_error())
    return buf.raw[:used.value]

def descriptor(kind,index=0,lang=0,length=255):
    data=query(0x220410,struct.pack('<IBBHHH',1,0x80,6,(kind<<8)|index,lang,length),12+length)
    return data[12:]

out={'parent':parent,'port':1}
try:
    info=query(0x220448,struct.pack('<I',1))
    out['connection_raw']=info.hex()
    d=info[4:22]
    out['device_descriptor_hex']=d.hex()
    out['device']={'usb_bcd':hex(int.from_bytes(d[2:4],'little')),'ep0_packet_bytes':d[7],
        'vid':hex(int.from_bytes(d[8:10],'little')),'pid':hex(int.from_bytes(d[10:12],'little')),
        'release_bcd':hex(int.from_bytes(d[12:14],'little')),'serial_index':d[16]}
    assert out['device']['vid']=='0x46d' and out['device']['pid']=='0xc077', 'Device changed; stop'
    out['speed_code']=info[23]
    config=descriptor(2,length=1024)
    out['config_hex']=config.hex()
    out['config_descriptors']=[]
    pos=0
    while pos+2<=len(config) and config[pos]>=2:
        item=config[pos:pos+config[pos]]
        out['config_descriptors'].append({'type':item[1],'hex':item.hex()})
        pos+=len(item)
    out['strings']={}
    string_indices = set(d[14:17]) | {config[6]}
    string_indices.update(bytes.fromhex(x['hex'])[8] for x in out['config_descriptors'] if x['type']==4)
    for idx in sorted(string_indices-{0}):
        try:
            raw=descriptor(3,idx,0x409)
            out['strings'][idx]=raw[2:raw[0]].decode('utf-16-le')
        except OSError as e:
            out['strings'][idx]=str(e)
finally:
    k.CloseHandle(handle)
Path(__file__).with_name('usb_probe.json').write_text(json.dumps(out,indent=2))
print(json.dumps(out,indent=2))

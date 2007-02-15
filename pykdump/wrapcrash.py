#
# -*- coding: latin-1 -*-
# Time-stamp: <07/02/15 15:06:09 alexs>

# Functions/classes used while driving 'crash' externally via PTY
# Most of them should be replaced later with low-level API when
# using Python loaded to crash as shared library
# There are several layers of API. Ideally, the end-users should only call
# high-level functions that do not depend on internal

# Copyright (C) 2006-2007 Alex Sidorenko <asid@hp.com>
# Copyright (C) 2006-2007 Hewlett-Packard Co., All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.



import sys
import string, re
import struct

import threading
import types
from StringIO import StringIO
import pprint

pp = pprint.PrettyPrinter(indent=4)

import tparser
import nparser

#GDBStructInfo = tparser.GDBStructInfo
GDBStructInfo = nparser.GDBStructInfo


import Generic as Gen
from Generic import BaseStructInfo

hexl = Gen.hexl

import crashspec
from crashspec import sym2addr, addr2sym

import LowLevel as ll
from LowLevel import getOutput, exec_gdb_command
exec_crash_command = getOutput

# GLobals used my this module

PYT_sizetype = {} 

	
# A well-known way to remove dups from sequence
def unique(s):
    u = {}
    for x in s:
        u[x] = 1
    return u.keys()



# Struct/Union info representation with methods to append data
class StructInfo(BaseStructInfo):
    def __init__(self, sname):
        BaseStructInfo.__init__(self, sname)
        # If command w/o explicit struct/union specifier does not work,
        # we'll try again
        try:
            sname = sname.strip()
        except:
            raise TypeError, "bad type " + str(sname)
        for pref in ('', 'struct ', 'union '):
            cmd = "ptype " + pref + sname
            rstr = exec_gdb_command(cmd)
            #print "CMD:", cmd, "\n", rstr
            if (rstr and rstr.find("type =") == 0): break
	#print "="*10, sname, "\n <%s>" % rstr
        # Check whether the return string is OK.
        # None if command fails
        if (rstr == None):
            errmsg = "The type <%s> does not exist in this dump" % sname
            raise TypeError, errmsg
	(stype, self.size, self.body) = GDBStructInfo(rstr)
        if (stype == 'struct' or stype == 'union'):
            self.stype = sname
        else:
            self.stype = stype
        # It is possible that self.stype now contains just one word 'struct' or
        # 'union', this can happen if typedef points to unnamed struct. In this
        # case it is better to leave the original type
        self.size = getSizeOf(self.stype)
        bitfieldpos = 0
        for f  in self.body:
            fname = f.fname
            f.parentstype = self.stype
            if (f.has_key("bitfield")):
                #print self.stype, fname
                f.offset = GDBmember_offset(self.stype, fname)
                f.bitoffset = bitfieldpos%8
                bitfieldpos += f.bitfield
            else:
                bitfieldpos = 0
                
	    self[fname] = f


# Artificial StructInfo - in case we don't have the needed symbolic info,
# we'd like to be able to assemble it manually

class ArtStructInfo(BaseStructInfo):
    def __init__(self, stype):
        BaseStructInfo.__init__(self, stype)
        # Add ourselves to cache
        Gen.addSI2Cache(self)

    # Append info. Reasonable approaches:
    # 1. Append (inline) an already existing structinfo.
    # 2. Append a field manually (do we need to parse its definition string?)
    #
    # In both cases we'll append with offset equal to self.size
    def append(self, obj):
        # If obj is a string, we'll need to parse it - not done yet
        if (type(obj) == types.StringType):
            f = tparser.OneStatement.parseString(obj).asList()[0]
            f.offset = self.size
            size = f.size
            if (size == -1):
                raise TypeError

            self.body.append(f)
            self[f.fname] = f 
            self.size += size
            #raise TypeError
        else:
            off = self.size
            # append adjusting offsets
            addsize = obj.size
            body = obj.body
            for f in body:
                fn = f.copy()           # We don't want to spoil the original
                try:
                    fn.offset += off
                except:
                    pass
                self.body.append(fn)
                self[fn.fname] = fn
            # Adjust the original size
            self.size += addsize

# Artificial StructInfo for Unions - in case we don't have the needed symbolic
# info, we'd like to be able to assemble it manually. We cannot use the same
# class for both Struct and Union as when we assemble them manually offsets
# are computed differently. Or maybe we can merge these two classes and
# use separate append_as_struct and append_as_union methods ?

class ArtUnionInfo(BaseStructInfo):
    def __init__(self, stype):
        BaseStructInfo.__init__(self, stype)
        # Add ourselves to cache
        Gen.addSI2Cache(self)

    # Append info. Reasonable approaches:
    # 1. Append a field manually (do we need to parse its definition string?)
    def append(self, obj):
        # If obj is a string, we'll need to parse it - not done yet
        if (type(obj) == types.StringType):
            f = tparser.OneStatement.parseString(obj).asList()[0]
            f.offset = 0
            size = f.size
            if (size == -1):
                raise TypeError

            self.body.append(f)
            self[f.fname] = f
            if (self.size < size):
                self.size = size
           

# An auxiliary class to be used in StructResult to process dereferences

class Dereference:
    def __init__(self, sr):
        self.sr = sr
    def __getattr__(self, f):
        # Get address from the struct
        addr = self.sr.__getattr__(f)
	if (addr == 0):
	    msg = "\nNULL pointer %s->%s" % (
	                                       str(self.sr), f)
	    raise IndexError, msg
        stype = self.sr.PYT_sinfo[f].basetype
        return readSU(stype, addr) 


# A cache to simplify access fo StructResult atributes. Indexed by
# (PYT_symbol, attr)
# Value is (type,  off, sz, signed)
# At this moment for 1-dim
# integer values only
_cache_access = {}

# Raw Struct Result - read directly from memory, lazy evaluation


# Warning: there can be a namespace collision if strcut/union of interest
# has a field with the same name as one of our methods. I am not sure
# whether we shall ever meet this but just in case there should be a method
# to bypass the normal accessor approach (GDBderef at this moment)
class StructResult(object):
    def __init__(self, sname, addr, data = None):
        # If addr is symbolic, convert it to real addr
        if (type(addr) == types.StringType):
            addr = sym2addr(addr)
	self.PYT_symbol = sname
	self.PYT_addr = addr
        self.PYT_sinfo = getStructInfo(sname)
        self.PYT_size = self.PYT_sinfo.size;
        self.PYT_deref = "Deref"
        if (data):
            self.PYT_data = data
        else:
            self.PYT_data = readmem(addr, self.PYT_size)
    
    def __getitem__(self, name):
        return self.PYT_sinfo[name]

    def __str__(self):
        return "<%s 0x%x>" % \
               (self.PYT_symbol, self.PYT_addr)
    def __repr__(self):
        return "StructResult <%s> \tsize=%d, addr=0x%x" % \
               (self.PYT_symbol, self.PYT_size, self.PYT_addr)
    def __nonzero__(self):
        return True
    def __len__(self):
        return self.PYT_size

    def hasField(self, fname):
        return self.PYT_sinfo.has_key(fname)
    
    def isNamed(self, sname):
	return sname == self.PYT_symbol
    
    # Cast to another type. Here we assume that that one struct resides
    # as the first member of another one, this is met frequently in kernel
    # sources
    def castTo(self, sname):
        newsize = struct_size(sname)
	# If the new size is smaller than the old one, reuse data
	if (newsize <= self.PYT_size):
	    # We don't truncate data as we rely on PYT_size
	    return StructResult(sname, self.PYT_addr, self.PYT_data)
	else:
	    return StructResult(sname, self.PYT_addr)
	

    # It is highly untrivial to read the field properly as there
    # are many subcases. We probably need to split it into several subroutines,
    # maybe internal to avoid namespace pollution
    def __getattr__(self, name):
        # First of all, try to use shortcut from cache
        cacheind = (self.PYT_symbol, name)
        try:
            itype, off, sz, signed = _cache_access[cacheind]
            s = self.PYT_data[off:off+sz]
            fieldaddr = self.PYT_addr + off
            #print "_cache_access", cacheind, itype
            if (itype == "Int"):
                return  mem2long(s, signed=signed)
            elif (itype == "String"):
                val = mem2long(s)
                if (val == 0):
                    val = None
                else:
                    s = readmem(val, 256)
                    val = SmartString(s, fieldaddr)
                return  val
            elif (itype == "CharArray"):
                return SmartString(s, fieldaddr)
            else:
                raise TypeError
        except KeyError:
            pass
        
        # A special case - dereference
        if (name == self.PYT_deref):
            return Dereference(self)
        try:
            ni = self.PYT_sinfo[name]
        except KeyError:
            # For some reason sk.__sk_common does not work as expected
            # when we do this inside a class methog. For example,
            # if we do this inside 'class IP_conn',
            # it passes to __getattr__  '_IP_conn__sk_common'
            # Python mangling is applied to class attributes starting from
            # two underscores - but why do we append _IP_Conn to an attribute
            # of StructResult?.
            ind = name.find('__')
            if (ind == -1):
                raise KeyError, name
            else:
                ni = self.PYT_sinfo[name[ind:]]

        #print "NI", ni
        sz = ni.size
        off = ni.offset
        # This can be an array...
        if (ni.has_key("array")):
            dim = ni.array
        else:
            dim = 1

        fieldaddr = self.PYT_addr + off
        reprtype = ni.smarttype
        stype = ni.basetype
	# Obtaining a slice of real data is rather expensive (as it needs
	# object creation)
        s = self.PYT_data[off:off+sz]
        #print "name, off,sz, smarttype", name, off,sz, reprtype

        # If this is an embedded SU, create a new struct object.
        # If struct type is not externally visible, i.e. crash/gdb cannot do
        # 'struct sname' we generate an 'internal' name
        if (reprtype == "SU"):
            # We can have here:
            # 1. 'struct AAA field'
            # 2. 'struct {...} field'
            # 3. 'type_s field'
            ftype = string.join(ni.type)
            # Ok, let us check whether this type is external
            if (struct_size(ftype) == -1 and not ni.typedef):
                # No, this is an internal one - maybe even without
                # a name
                s_u = ni.type[0]
                if (s_u == "struct"):
		    fakename = self.PYT_symbol+'-'+ftype+'-'+name
		    # Check whether it already has been created
                    try:
                        au = getStructInfo(fakename)
                    except TypeError:
                        au = ArtStructInfo(fakename)
                        for fi in ni.body:
                            au.append(fi.cstmt)
                        #print au
                    val = StructResult(fakename, fieldaddr)
                    return val
                elif (s_u == "union"):
                    #print "NI", ni
                    fakename = self.PYT_symbol+'-'+ftype+'-'+name
                    # Check whether it already has been created
                    try:
                        au = getStructInfo(fakename)
                    except TypeError:
                        au = ArtUnionInfo(fakename)
                        for fi in ni.body:
                            au.append(fi.cstmt)
                        #print au
                    val = StructResult(fakename, fieldaddr)    
                    return val
                else:
                    raise TypeError, s_u
                
            
            # This can be an array...
            if (dim != 1):
                # We sometimes meet dim=0, e.g.
                # struct sockaddr_un name[0];
                # I am not sure about that but let us process by
                # loading new data from this address
                if (dim == 0):
                    #print "dim=0", ftype, hexl(self.PYT_addr + off)
                    val = StructResult(ftype, fieldaddr)
                else:
                    sz1 = sz/dim
                    val = []
                    for i in range(0, dim):
                        s1 = s[i*sz1:(i+1)*sz1]
                        one =  StructResult(ftype, fieldaddr+i*sz1, s1)
                        val.append(one)
            else:
		# We return this in case of SU with an 'external' type
                val = StructResult(ftype, fieldaddr, s)
            # ------- end of SU --------------------
        elif (reprtype == "CharArray"):
	    if (dim == 0):
                # We assume we should return a pointer to this offset
                val = fieldaddr
	    else:
                # Return it as a string - may contain ugly characters!
                # not NULL-terminated like String reprtype
                val = SmartString(s, fieldaddr)
                _cache_access[cacheind] = ("CharArray", off, sz, False)
        elif (reprtype == "String" and dim == 1):
            val = mem2long(s)
            if (val == 0):
                val = None
            else:
                s = readmem(val, 256)
                val = SmartString(s, fieldaddr)
            _cache_access[cacheind] = ("String", off, sz, False)
        else:
            # ----- A kitchen sink: integer types --------
            signed = False
            if (reprtype == 'Char' or reprtype == 'SInt'):
                signed = True
            #print name, reprtype, sz, dim,  signed
            if (dim == 1):
                val = mem2long(s, signed=signed)
                # Are we a bitfield??
                if (ni.has_key("bitfield")):
                    val = (val&(~(~0<<ni.bitoffset+ ni.bitfield)))>>ni.bitoffset
                # Are we SUptr?
                elif (reprtype == "SUptr"):
                    val =  _SUPtr(val)
                    val.sutype = stype
                else:
                    pass
                    _cache_access[(self.PYT_symbol, name)]=("Int", off, sz, signed)
            elif (dim == 0):
                # We assume we should return a pointer to this offset
                val = fieldaddr
            else:
                val = []
                sz1 = sz/dim
                for i in range(0,dim):
                    val1 = mem2long(s[i*sz1:(i+1)*sz1], signed=signed)
                    #val1 = mem2long(s, i*sz1, sz1)
                    # Are we SUptr?
                    if (reprtype == "SUptr"):
                        val1 =  _SUPtr(val1)
                        val1.sutype = stype

                    val.append(val1)

            # OK, if this is pointer to char, retrieve the string. To prevent
            # problems because of bogus strings, do no retrieve more than 256 bytes
        #self.__dict__[name] = val
        return val
    
    # An ugly hack till we implement something better
    # We want to be able to do something like
    # s.GDBderef('->addr->name->sun_path')
    #
    def GDBderef(self, derefexpr):
        # Use our type and addr
        cmd = "p ((%s *) 0x%x)%s"%(self.PYT_sinfo.stype, self.PYT_addr, derefexpr)
        #print cmd
        resp = exec_gdb_command(cmd)
        f = tparser.derefstmt.parseString(resp).asList()[0]
        return f

# Wrapper functions to return attributes of StructResult

def Addr(obj, extra = None):
    if (isinstance(obj, StructResult)):
        # If we have extra set, we want to know the address of this field
        if (extra == None):
            return obj.PYT_addr
        else:
            off = obj.PYT_sinfo[extra].offset
            return obj.PYT_addr + off
    elif (isinstance(obj, SmartString)):
          return obj.addr
    else:
        raise TypeError

# When we do readSymbol and have pointers to struct, we need a way
# to record this info instead of just returnin integer address

class _SUPtr(long):
    def getDeref(self):
        return readSU(self.sutype, self)
    Deref = property(getDeref)


from UserString import UserString
class SmartString(UserString):
    def __init__(self, s, addr = None):
        UserString.__init__(self, s.split('\0')[0])
        self.addr = addr
        self.fullstr = s


# Print the object delegating all work to GDB. At this moment can do this
# for StructResult only

def printObject(obj):
    if (isinstance(obj, StructResult)):
        cmd = "p *(%s *)0x%x" %(obj.PYT_symbol, obj.PYT_addr)
        print cmd
        s = exec_gdb_command(cmd)
        # replace the 1st line with something moe useful
        first, rest = s.split("\n", 1)
	print "%s 0x%x {" %(obj.PYT_symbol, obj.PYT_addr)
        print rest
    else:
        raise TypeError
        

# =============================================================
#
#           ======= read functions =======
#
# =============================================================

# Read a pointer-size value from addr
def readPtr(addr):
    s = readmem(addr, pointersize)
    return mem2long(s)

def readU16(addr):
    s = readmem(addr, 2)
    return mem2long(s)

def readU32(addr):
    s = readmem(addr, 4)
    return mem2long(s)
    
# addr should be numeric here
def readSU(symbol, addr):
    return StructResult(symbol, addr)

#          ======== read arrays =========

# Read an array of pointers from a given symbol
def readPointerArray(symbol):
    si = whatis(symbol)
    if (si.has_key("array")):
        dim = si.array
    else:
        dim = 1
    stype = string.join(si.type)
    addr = sym2addr(symbol)
    s = readmem(addr, pointersize*dim)
    out = []
    for i in range(0, dim):
        val = mem2long(s[i*pointersize:(i+1)*pointersize])
        out.append(val)
    return out

# Read an array of structs/unions given the structname, start and dimension
def readSUArray(suname, startaddr, dim=0):
    # If dim==0, return a Generator
    if (dim == 0):
        return SUArray(suname, startaddr)
    sz = struct_size(suname)
    # Now create an array of StructResult.
    out = []
    for i in range(0,dim):
        out.append(StructResult(suname, startaddr+i*sz))
    return out


#          ======== read lists  =========


# Emulate list_for_each + list_entry
# We assume that 'struct mystruct' contains a field with
# the name 'listfieldname'
# Finally, by default we do not include the address f the head itself
#
# If we pass a string as 'headaddr', this is the symbol pointing
# to structure itself, not its listhead member
def readSUListFromHead(headaddr, listfieldname, mystruct, maxel=1000,
                     inchead = False):
    msi = getStructInfo(mystruct)
    offset = msi[listfieldname].offset
    if (type(headaddr) == types.StringType):
        headaddr = sym2addr(headaddr) + offset
    out = []
    for p in readList(headaddr, 0, maxel, inchead):
        out.append(readSU(mystruct, p - offset))
    return out

# Read a list of structures connected via direct next pointer, not
# an embedded listhead. 'shead' is either a structure or _SUptr pointer
# to structure

def readStructNext(shead, nextname):
    if (not isinstance(shead, StructResult)):
        if (shead == 0):
            return []
        else:
            shead = shead.Deref
    stype = shead.PYT_symbol
    offset = shead.PYT_sinfo[nextname].offset
    out = []
    for p in readList(Addr(shead), offset):
        out.append(readSU(stype, p))
    return out 

#     ======= return a Generator to iterate through SU array
def SUArray(sname, addr, maxel = 10000):
    size = getSizeOf(sname)
    addr -= size
    while (maxel):
        addr += size
        yield readSU(sname, addr)
    return

#     ======= read from global according to its type  =========



# Try to read symbol according to its type and return the appropriate object
# For example, if this is a struct, return StructObj, if this is an array
# of Structs, return a list of StructObj

def readSymbol(symbol, art = None):
    symi = whatis(symbol, art)
    stype = symi.basetype
    swtype = symi.smarttype
    addr = sym2addr(symbol)

    # This can be an array...
    if (symi.has_key("array")):
        dim = symi.array
    else:
        dim = 1

    size = getSizeOf(symbol)
    # There is a special case - on some kernels we obtain zero-dimensioned
    # arrays, e.g. on 2.6.9 sizeof(ipv4_table) = 0 and it ise declared as
    # ctl_table ipv4_table[] = {...}
    # In this case we return a generator to this array and expect that
    # there is an end marker that lets programmer detect EOF. For safety
    # reasons, we limit the number of returned entries to 10000
    if (dim == 0 and size == 0 and swtype == "SU"):
        sz1 = getSizeOf(stype)
        return SUArray(stype, addr)

    sz1 = size/dim

    s = readmem(addr, size)
    
    #print "ctype=<%s> swtype=<%s> dim=%d" % (symi.ctype, swtype, dim)
    if (swtype == "SU"):
        #elsi = getStructInfo(stype)
        #size = elsi.size
        if (dim > 1):
            out = []
            for i in range(0,dim):
                out.append(readSU(stype, addr+i*sz1))
            return out
        else:
            return readSU(stype, addr)
    elif ((swtype == "Ptr" or swtype == "SUptr") and dim > 1):
        out = []
        for i in range(0,dim):
            ptr =  mem2long(s[i*pointersize:(i+1)*pointersize])
            if (swtype == "SUptr"):
                ptr = _SUPtr(ptr)
                ptr.sutype = stype
            out.append(ptr)
        return out
    elif (swtype == "SInt"):
        if (dim == 1):
            return mem2long(s, signed=True)
        else:
            out = []
            for i in range(0, dim):
                val = mem2long(s[i*pointersize:(i+1)*pointersize], signed=True)
                out.append(val)
            return out
    elif (swtype == "UInt" or swtype == "UChar" or 
          swtype == "Ptr" or swtype == "SUptr"):
        if (dim == 1):
            addr =  mem2long(s)
            if (swtype == "SUptr"):
                ptr = _SUPtr(addr)
                ptr.sutype = stype
                return ptr
            else:
                return addr
        else:
            out = []
            for i in range(0, dim):
                val = mem2long(s[i*sz1:(i+1)*sz1])
                if (swtype == "SUptr"):
                    val = _SUPtr(val)
                    val.sutype = stype
                out.append(val)
            return out
    else:
        return None


# Get sizeof(type)
def getSizeOf(vtype):
    try:
        return PYT_sizetype[vtype]
    except KeyError:
        sz = GDB_sizeof(vtype)
        PYT_sizetype[vtype] = sz
        return sz

#int readmem(ulonglong addr, int memtype, void *buffer, long size,
#	char *type, ulong error_handle)
# memtype:
#     UVADDR
#     KVADDR
#     PHYSADDR

# Non-cached version
def ncreadmem(addr, size, memtype = 'KVADDR'):
    if (memtype != 'KVADDR'):
        print "Cannot read anything but KVADDR w/o extension"
        sys.exit(1)
    return dumpMemory(addr, addr+size)

# Cached version - do not use on live kernels
def creadmem(addr, size, memtype = 'KVADDR'):
    if (memtype != 'KVADDR'):
        print "Cannot read anything but KVADDR w/o extension"
        sys.exit(1)
    return cdumpMemory(addr, addr+size)


# By default, we use a cached version - the difference can be 2-3 times!
readmem = creadmem


# .........................................................................
import time
def readFifo(func, *args, **kwargs):
    res = []
    def readFifo():
        fd = open(fifoname, "r")
        res.append(fd.read())
        #print "FIFO read"
        fd.close()
    mt = threading.Thread(target=readFifo)
    mt.start()
    func(*args, **kwargs)
    mt.join()
    return res[0]

# Dump memory and get it
def dumpMemory(start, stop):
    command = "dump memory %s 0x%x 0x%x" % (fifoname, start, stop)
    #print command
    res = readFifo(exec_gdb_command, command)
    #print "Line sent"
    return res

# 8K - pages
shift = 12
psize = 1 << shift
_page_cache = {}

# Dump memory with page cache
def cdumpMemory(start, stop):
    pstart = start>>shift
    pstop = stop>>shift
    if (pstart == pstop):
        pagestart = pstart << shift
        try:
            page = _page_cache[pstart]
            #print "cpage pstart=0x%x len=%d" % (pstart, len(page))
        except:
            page = dumpMemory(pagestart, pagestart+psize)
            _page_cache[pstart] = page
        return page[start - pagestart:stop-pagestart]
    else:
        return dumpMemory(start, stop)

# Flush cache (for tools running on a live system)
def flushCache():
    _page_cache.clear()
    
# ..............................................................

# Convert raw memory of proper size to int/long

# 1/2/4/8 sizes are OK both for 32-bit and 64-bit systems:
# 1 - unsigned char
# 2 - unsigned short
# 4 - unsigned int
# 8 - long long
#
# But I am not sure about IA64 - need to check

ustructcodes32 = [0, 'B', 'H', 3, 'I', 5, 6, 7, 'Q']
structcodes32 = [0, 'b', 'h', 3, 'i', 5, 6, 7, 'q']

def mem2long(s, signed=False):
    sz = len(s)
    if(signed):
        val = struct.unpack(structcodes32[len(s)], s)[0]
    else:
        val = struct.unpack(ustructcodes32[len(s)], s)[0]
    return val

    
# Get a list of non-empty bucket addrs (ppointers) from a hashtable.
# A hashtable here is is an array of buckets, each one is a structure
# with a pointer to next structure. On 2.6 'struct hlist_head' is used
# but we don't depend on that, we just need to know the offset of the
# 'chain' (a.k.a. 'next') in our structure
#
# start - address of the 1st hlist_head
# bsize - the size of a structure embedding hlist_head
# items - a dimension of hash-array
# chain_off - an offset of 'hlist_head' in a bucket
def getFullBuckets(start, bsize, items, chain_off=0):
    chain_sz = pointersize
    m = readmem(start, bsize * items)
    buckets = []
    for i in xrange(0, items):
       chain_s = i*bsize + chain_off
       s = m[chain_s:chain_s+chain_sz]
       bucket = mem2long(s)
       #bucket = mem2long(m, chain_sz, chain_s, False)
       if (bucket != 0):
           #print i
           buckets.append(bucket)
    del m
    return buckets

# Traverse list_head linked lists


# d.emulateCrashList('block_device.bd_disk', 'block_device.bd_list', 'all_bdevs')
#                        what to get               list_head              symbol

def emulateCrashList(off_need, off_list, addr, maxel=1000):
    # All arguments can be either symbolic or integer. In case the first two
    # are integer we expect them to specify the same structure
    if (type(off_need) == types.StringType):
        (sname, fname) = off_need.split('.')
        si = getStructInfo(sname)
        off_need = si[fname].offset
    if (type(off_list) == types.StringType):
        (sname, fname) = off_list.split('.')
        si = getStructInfo(sname)
        off_list = si[fname].offset
    if (type(addr) == types.StringType):
        addr = sym2addr(addr)

    offset = off_need - off_list
    ptrs = readList(addr, 0, 1000)
    # Now recompute so that we'll point to struct of interest
    ptrs = [(p - off_list, readPtr(p+offset)) for p in ptrs]
    return ptrs


def getStructInfo(stype, createnew = True):
    for pref in ('', 'struct ', 'union '):
        try:
            return Gen.getSIfromCache(pref+stype)
        except:
            if (not createnew):
                raise TypeError, "Unknown Type <%s>" % stype
            pass
    #print "  -- SI Cache miss:", stype
    si = StructInfo(stype)
    Gen.addSI2Cache(si)
    #.__sinfo_cache[stype] = si
    return si

__whatis_cache = {}
#re_gdb_whatis = re.compile(r'(.+)(\[\d+\])$')
re_gdb_whatis = re.compile('([^[]+)(.*)$')
# Whatis command
def whatis(symbol, art=None):
    global __whatis_cache
    try:
        return __whatis_cache[symbol]
    except:
        pass
    if (art == None):
	resp = exec_gdb_command('whatis ' + symbol)
        # if resp is None, there's no symbol like that
        if (resp == None):
            raise TypeError, "There's no symbol <%s>" % symbol
	# 'gdb whatis' is different from just 'whatis': identifier is not there.
        # E.g.
	# crash> whatis chrdevs
	# struct char_device_struct *chrdevs[255];
	#crash> gdb whatis chrdevs
	#type = struct char_device_struct *[255]
        #type = struct list_head [32][8]   => struct list_head nf_hooks[32][8];
	resp = resp.split('=', 1)[1]
	m = re_gdb_whatis.match(resp)
	if (m):
	    resp = m.group(1) + ' ' + symbol + m.group(2) + ";"
	else:
	    resp = resp + ' ' + symbol + ";"
    else:
	resp = art
    f = tparser.OneStatement.parseString(resp).asList()[0]
    
    # A special case: we have a nameless global struct.
    # In this case body=['...'].
    if (f.has_key('body') and f['body'][0] == '...'):
        # Create an Artificial struct with the name GLOB-symbol
        artname = "struct GLOB-"+symbol
        as = ArtStructInfo(artname)
        fields =  GDB_ptype(symbol)[2]
        for af in fields:
            as.append(af.cstmt)
        del f['body']
        f.type = artname.split()
    elif (len(f.type) == 1):
        # If our type consists of a single word and it is not struct/union,
        # this is probably a typedef (or basic type).
        # Try to obtain more info in this case
        newtype = getTypedefInfo(f.basetype)
        if (newtype):
            # typedef may be mapped to a base type or to pointer to basetype
            # count and remove stars. At this moment GDB glues all stars, e.g.
            # gdb ptype int* * * => type = int ***
            #print newtype
            spl = newtype.split()

            if (spl[-1][0] == '*'):
                stars = spl[-1]
                f.type = spl[0:-1]
                if (f.has_key('star')):
                    f.star += stars
                else:
                    f.star = stars
            else:
                f.type = spl
    __whatis_cache[symbol] = f 
    return f

# Check whether our basetype is really a typedef. We need this to understand how
# to generate 'smarttype'. E.g. for __u32 we'll find that this is an unsigned integer
# For typedefs to pointers we'll know that this is really a pointer type and should
# be treated as such.
# Possible return values:
#           None    - this is not a typedef, not transformation possible
#           Int     - this is a signed Integer type
#           Uint    - this is a Unsigned integer type
#           Ptr     - this is a pointer, do not try to do anything else
#           SUPtr   - this is a pointer to SU
#           String  - this is a pointer to Char

def isTypedef(basetype):
    # If there is a 'struct' or 'union' word, it does not make sense to continue
    spl = basetype.split()
    if ('struct' in spl or 'union' in spl):
        return None
    newtype = getTypedefInfo(basetype)
    if (newtype == None or newtype == basetype):
        # No need not modify anything
        return None
    return newtype



# Walk list_Head and return the full list (or till maxel)
#
# Note: By default we do not include the 'start' address.
# This emulates the behavior of list_for_each_entry kernel macro.
# In most cases the head is standalone and other list_heads are embedded
# in parent structures.

def readListByHead(start, offset=0, maxel = 1000):
    return readList(start, offset, maxel, False)

# An alias
list_for_each_entry = readListByHead

# readList returns the addresses of all linked structures, including
# the start address. If the start address is 0, it returns an empty list

def readList(start, offset=0, maxel = 1000, inchead = True):
    if (start == 0):
        return []
    count = 1
    if (inchead):
        out = [start]
    else:
        out = []
    next = start
    while (count < maxel):
        next = readPtr(next + offset)
        if (next == 0 or next == start):
            break
        out.append(next)
        count += 1
    return out


#
#
#  -- emulating low-level functions that can be later replaced by
#  Python extension to crash
#
#
# {"symbol_exists",  py_crash_symbol_exists, METH_VARARGS},
# {"struct_size",  py_crash_struct_size, METH_VARARGS},
# {"union_size",  py_crash_union_size, METH_VARARGS},
# {"member_offset",  py_crash_member_offset, METH_VARARGS},
# {"member_size",  py_crash_member_size, METH_VARARGS},
# {"get_symbol_type",  py_crash_get_symbol_type, METH_VARARGS},


# Return -1 if the struct is unknown
def struct_size(sname):
    try:
        si = getStructInfo(sname)
        return si.size
    except:
        return -1

def struct_exists(sname):
    if (struct_size(sname) == -1):
        return False
    else:
        return True
    
def member_size(sname, fname):
    #print "++member_size", sname, fname
    sz = -1
    try:
        fi = getStructInfo(sname)[fname]
        sz = fi.size
        if (fi.has_key("array")):
            sz *= fi.array
    except:
        pass
    return sz


# Find a member offset. If field name contains a dot, we do our
# best trying to find its offset checking intermediate structures as
# needed

def member_offset(sname, fname):
    try:
        si = getStructInfo(sname)
        if (fname.find('.') == -1):
            return si[fname].offset
        else:
            # We have dots in field name, try to walk the structures
            return -1                   # Not done yet
    except:
        return -1


# A trick like that does not always work as GDB tries to validate the address
# p &(((struct sock *)0)->sk_rcvbuf)
# But if we use a 'good' base address it seems to work fine

goodbase = 0
def GDBmember_offset(sname, fname):
    global goodbase
    if (not goodbase):
        goodbase = sym2addr("tcp_hashinfo")
    cmd = "p (unsigned long)(&(((%s *)0x%x)->%s))" % (sname, goodbase, fname)
    rs = exec_gdb_command(cmd)
    #print "cmd=<%s>, rs=<%s>" % (cmd, rs)
    if (rs[0] != '$'):
        offset = -1
    else:
        offset = int(int(rs.split("=")[1]) - goodbase)
    #print "Offset for %s.%s = %d" % (sname, fname, offset)
    return offset
    

# GDB version of symbol_exists

#crash> gdb info address tcp_hashinfo
#Symbol "tcp_hashinfo" is static storage at address 0xc038e180.
#crash> gdb info address tcp_hashinfo1
#No symbol "tcp_hashinfo1" in current context.

# WARNING: sometimes the symbol is visible using 'sym' command but not GDB command
# E.g.:
#crash> sym udpv6_protocol
#f8b835a0 (d) udpv6_protocol
#crash> gdb info address udpv6_protocol
#No symbol "udpv6_protocol" in current context.


def GDB_symbol_exists(sym):
    s = exec_gdb_command("info address " + sym)
    if (s == None or s[0] == 'N'):
        return 0
    else:
        return 1

# Uncached GDB version
def GDB_sizeof(vtype):
    command = "p sizeof(%s)" % vtype
    rs = exec_gdb_command(command)
    #print 'vtype=<%s> rs=<%s>' % (vtype, rs)
    if ((not rs) or rs[0] != '$'):
        sz = -1
    else:
        sz = int(rs.split('=')[1])
    return sz
 
# GDB version of 'ptype'
def GDB_ptype(sym):
    rstr = exec_gdb_command("ptype " + sym)
    return GDBStructInfo(rstr)

re_ptype = re.compile('^type = ([^{]+)\s*{*$')
def getTypedefInfo(tname):
    rstr = exec_gdb_command("ptype " + tname).split('\n')[0]
    # If we are OK, the 1st line is something like
    # 'type = struct sock {'
    # or type = unsigned int
    m = re_ptype.match(rstr)
    if (m):
        # Typedef may include *
        return m.group(1).strip()
    else:
        return None

# Return either a simple string or a StructInfo object
re_ptype_new = re.compile('^type = ([^{]+)\s*$')
def getTypedefInfo_new(tname):
    try:
        rstr = exec_gdb_command("ptype " + tname)
    except:
        return None
    # If we are OK, the 1st line is something like
    # 'type = struct sock {'
    # or type = unsigned int
    m = re_ptype.match(rstr)
    if (m):
        # Typedef may include *
        return m.group(1).strip()
    else:
        try:
            (stype, size, body) = GDBStructInfo(rstr)
            size = getSizeOf(stype)
            # If typedef is to an unnamed struct, there is a chance that typedef
            # size is known
            if (size == -1):
                size = getSizeOf(tname)
            return (stype, size, body)
        except:
            return None

noncached_symbol_exists = crashspec.symbol_exists

# A cached version
__cache_symbolexists = {}
def symbol_exists(sym):
    try:
        return  __cache_symbolexists[sym]
    except:
        rc = noncached_symbol_exists(sym)
        __cache_symbolexists[sym] = rc
        return rc
    


# Aliases
union_size = struct_size

# Some functions can be replaced with more efficient low-level ones...
try:
    import crash
    from crash import sym2addr, addr2sym
    from crash import  mem2long
    def exec_gdb_command(cmd):
        return crash.get_GDB_output(cmd).replace('\r', '')

    noncached_symbol_exists = crash.symbol_exists
    exec_crash_command = crash.exec_crash_command
    exec_gdb_command = crash.get_GDB_output
    getFullBuckets = crash.getFullBuckets
    readPtr = crash.readPtr
    # For some reason the next line runs slower than GDB version
    #GDB_sizeof = crash.struct_size
    readmem = crash.readmem
    GDBmember_offset = crash.member_offset
except:
    pass

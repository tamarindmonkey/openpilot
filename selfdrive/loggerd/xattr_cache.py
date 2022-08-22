# import os
import errno
from typing import Dict, Tuple, Optional
from common.xattr import getxattr as getattr1
from common.xattr import setxattr as setattr1

_cached_attributes: Dict[Tuple, Optional[bytes]] = {}

def getxattr(path: str, attr_name: str) -> Optional[bytes]:
  key = (path, attr_name)
  if key not in _cached_attributes:
    try:
      response = getattr1(path, attr_name)
    except OSError as e:
      # ENODATA means attribute hasn't been set
      if e.errno == errno.ENODATA:
        response = None
      else:
        raise
    _cached_attributes[key] = response
  return _cached_attributes[key]

def setxattr(path: str, attr_name: str, attr_value: bytes) -> None:
  _cached_attributes.pop((path, attr_name), None)
  return setattr1(path, attr_name, attr_value)

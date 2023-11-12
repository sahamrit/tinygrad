from __future__ import annotations
import functools, operator
from dataclasses import dataclass
from typing import Tuple, List, Optional, Dict, cast
from tinygrad.helpers import prod, all_int, dedup
from tinygrad.shape.symbolic import Node, NumNode, Variable, VariableOrNum, is_sym_int, sint

@functools.lru_cache(maxsize=None)
def filter_strides(shape:Tuple[int, ...], strides:Tuple[int, ...]) -> Tuple[int, ...]:
  return tuple(stride if shp != 1 else 0 for stride, shp in zip(strides, shape))

@functools.lru_cache(maxsize=None)
def strides_for_shape(shape:Tuple[int, ...]) -> Tuple[int, ...]:
  strides = [1] if shape else []
  for d in shape[::-1][:-1]: strides = [d*strides[0]] + strides
  return filter_strides(shape, tuple(strides))

@functools.lru_cache(maxsize=None)
def to_shape_strides(_shape:Tuple[int, ...], _strides:Tuple[int, ...], _mask:Tuple[Tuple[int, int], ...] = None) -> Tuple[Tuple[int, int, int], ...]:
  assert len(_shape) == len(_strides)
  shape = [s for idx, s in enumerate(_shape) if _shape[idx] != 1]
  strides = [s for idx, s in enumerate(_strides) if _shape[idx] != 1]
  mask = [s for idx, s in enumerate(_mask) if _shape[idx] != 1] if _mask else None
  mask_stride_0 = mask and mask[0][1] - mask[0][0] == 1 and strides[0] == 0
  ret = [(shape[0], strides[0], mask[0][0] if len(shape) != 1 and mask_stride_0 else 0)] if shape else ([(1, 0, 0)] if len(_shape) else [])
  state = 1 if mask_stride_0 else 0
  for i in range(1, len(shape)):
    if mask and strides[i] == 0 and mask[i][1] - mask[i][0] == 1 and i != len(shape) - 1:
      if state == 1:
        ret[-1] = (ret[-1][0] * shape[i], strides[i], ret[-1][2] * shape[i]  + mask[i][0])
      else:
        ret.append((ret[-1][0] * shape[i], strides[i], mask[i][0])); state = 1
    elif state == 1:
      ret[-1] = (ret[-1][0] * shape[i], strides[i], ret[-1][2] * shape[i]); state = 2
    elif (ret[-1][1] == shape[i] * strides[i] or ret[-1][0] == 1) and state != 2:
      ret[-1] = (ret[-1][0] * shape[i], strides[i], 0); state = 0
    else:
      ret.append((shape[i], strides[i], 0)); state = 0
  return tuple(ret)

@functools.lru_cache(maxsize=None)
def _reshape_mask(view: View, new_shape:Tuple[sint, ...]) -> Tuple[Optional[Tuple[Tuple[sint, sint], ...]],bool]:
  # assumes view can be reshaped to new_shape (if it had no mask), this implies we won't have to worry about strides
  if view.mask is None: return view.mask, False
  new_mask: List[Tuple[int, int]] = []
  r_masks, r_shape, r_new_shape = reversed(view.mask), reversed(view.shape), reversed(new_shape)
  stride, old_dim, new_dim, mask = 1, next(r_shape, 1), next(r_new_shape, 1), next(r_masks, (0,1))
  while len(new_mask) < len(new_shape):
    if mask[1]-mask[0] < 1: # if the mask is never valid, just return all zeros
      return ((0,0),)*len(new_shape), False
    if old_dim == new_dim*stride: # easy, can just copy the mask
      new_mask.append((mask[0]//stride, (mask[1]-1)//stride+1))
      stride, old_dim, new_dim, mask = 1, next(r_shape, 1), next(r_new_shape, 1), next(r_masks, (0,1))
    elif old_dim > new_dim: # splitting the old mask
      # we cannot split if the reshape cuts across the mask
      if (mask[0]%(new_dim*stride)!=0 or mask[1]%(new_dim*stride)!=0) and mask[0]//(new_dim*stride)!=(mask[1]-1)//(new_dim*stride):
        return view.mask, True
      new_mask.append((mask[0]%(new_dim*stride)//stride, (mask[1]-1)%(new_dim*stride)//stride+1))
      # the remaining mask still needs to be split, we need to determine the mask for the next dimension  
      # we maintain the stride 
      stride *= new_dim
      new_dim = next(r_new_shape, 1)
    elif old_dim < new_dim*stride: # combining masks
      next_mask = next(r_masks, (0,1)) 
      # if the current dimension is masked, we cannot merge unless the next masks have an index range of 1
      if (mask[0]!=0 or mask[1]!=old_dim) and next_mask[1]-next_mask[0]!=1:
        return view.mask, True
      # we combine the current mask with the next and go through the loop again with the next dimension
      mask = (next_mask[0]*old_dim+mask[0], (next_mask[1]-1)*old_dim+mask[1])
      old_dim *= next(r_shape, 1) 
  for mask in r_masks: # if the old shape has leading 1s, need to make sure their mask is (0,1), otherwise the mask is zero'd
    if mask != (0,1): return ((0,0),)*len(new_shape), False
  return tuple(reversed(new_mask)), False

@dataclass(frozen=True)
class View:
  shape:Tuple[sint, ...]
  strides:Tuple[sint, ...]
  offset:sint
  mask:Optional[Tuple[Tuple[sint, sint], ...]]
  contiguous:bool

  @staticmethod
  @functools.lru_cache(maxsize=None)
  def create(shape:Tuple[sint, ...], strides:Optional[Tuple[sint, ...]]=None, offset:sint=0, mask:Optional[Tuple[Tuple[sint, sint], ...]]=None):
    strides = filter_strides(shape, strides) if strides else strides_for_shape(shape)
    contiguous = offset == 0 and mask is None and all(s1 == s2 for s1,s2 in zip(strides, strides_for_shape(shape)))
    return View(shape, strides, offset, mask, contiguous)

  @functools.lru_cache(maxsize=None)  # pylint: disable=method-cache-max-size-none
  def size(self): return prod([s.max if isinstance(s, Node) else s for s,st in zip(self.shape, self.strides) if st != 0])

  def vars(self) -> List[Variable]:
    flatten_mask = tuple(x for m in self.mask for x in m) if self.mask is not None else tuple()
    return dedup(functools.reduce(operator.add, [x.vars() for x in self.shape+self.strides+(self.offset,)+flatten_mask if isinstance(x, Node)], []))

  def unbind(self) -> View:
    unbound_vars:Dict[VariableOrNum,Node] = {v: v.unbind()[0] for v in self.vars() if v.val is not None}
    new_shape = tuple([s if isinstance(s, int) else s.substitute(unbound_vars) for s in self.shape])
    new_strides = tuple([s if isinstance(s, int) else s.substitute(unbound_vars) for s in self.strides])
    new_offset = self.offset if isinstance(self.offset, int) else self.offset.substitute(unbound_vars)
    new_mask = tuple((a if isinstance(a, int) else a.substitute(unbound_vars), b if isinstance(b, int) else b.substitute(unbound_vars)) for (a, b) in self.mask) if self.mask is not None else None
    return View.create(new_shape, new_strides, new_offset, new_mask)

  # MovementOps live here now

  def __unsafe_resize(self, arg: Tuple[Tuple[sint, sint], ...], mask=None) -> View:
    offset = sum([s * x[0] for s, x in zip(self.strides,arg)])
    if self.mask:
      # move the old mask
      nmask = tuple([(max(mx-ax, 0), min(my-ax, ay-ax)) for (mx,my),(ax,ay) in zip(self.mask, arg)])
      # merge the masks if we have two
      mask = tuple([(max(mx1, mx2), min(my1, my2)) for (mx1, my1), (mx2, my2) in zip(nmask, mask)]) if mask is not None else nmask
    shape = [y-x for x,y in arg]
    return View.create(tuple(s.b if isinstance(s, NumNode) else s for s in shape), self.strides, self.offset+offset, mask)

  @functools.lru_cache(maxsize=None)  # pylint: disable=method-cache-max-size-none
  def pad(self, arg: Tuple[Tuple[int, int], ...]) -> View:
    assert all((b>=0 and e>=0) for b,e in arg) and len(arg) == len(self.shape)
    if any(b or e for b, e in arg):
      zvarg = tuple([(-b,s+e) for s,(b,e) in zip(self.shape, arg)])
      mask = tuple([(b,s+b) for s,(b,_) in zip(self.shape, arg)])
      return self.__unsafe_resize(zvarg, mask=mask)
    return self

  @functools.lru_cache(maxsize=None)  # pylint: disable=method-cache-max-size-none
  def shrink(self, arg: Tuple[Tuple[sint, sint], ...]) -> View:
    assert all((b>=0 and e<=s) for s,(b,e) in zip(self.shape,arg)) and len(arg) == len(self.shape)
    return self.__unsafe_resize(arg)

  @functools.lru_cache(maxsize=None)  # pylint: disable=method-cache-max-size-none
  def expand(self, new_shape: Tuple[sint, ...]) -> View:
    assert len(new_shape) == len(self.shape)
    assert all(is_sym_int(x) and (s == x or (s == 1 and st == 0)) for s,x,st in zip(self.shape, new_shape, self.strides)), f"can't expand {self.shape} into {new_shape}"
    # NOTE: can the mask ever be (0,0)?
    mask = tuple([(((0,0) if m != (0,1) else (0,ns)) if s != ns else m) for m,s,ns in zip(self.mask, self.shape, new_shape)]) if self.mask else None
    return View.create(new_shape, self.strides, self.offset, mask)

  @functools.lru_cache(maxsize=None)  # pylint: disable=method-cache-max-size-none
  def permute(self, axis: Tuple[int, ...]) -> View:
    assert all(isinstance(x, int) and x >= 0 and x < len(self.shape) for x in axis), f"invalid permute {axis} for {self.shape}"
    assert len(set(axis)) == len(axis) and len(axis) == len(self.shape), f"can't permute {self.shape} with {axis}"
    return View.create(tuple([self.shape[a] for a in axis]), tuple([self.strides[a] for a in axis]), self.offset, tuple([self.mask[a] for a in axis]) if self.mask is not None else None)

  @functools.lru_cache(maxsize=None)  # pylint: disable=method-cache-max-size-none
  def stride(self, mul: Tuple[int, ...]) -> View:
    # except for the negative case, you can build this from the others. invertible in the negative case
    assert all(isinstance(x, int) and x != 0 for x in mul), f"invalid stride {mul} for {self.shape}"
    strides = tuple([z*m for z,m in zip(self.strides, mul)])
    new_shape = tuple([(s+(abs(m)-1))//abs(m) for s,m in zip(self.shape, mul)])
    offset = sum([(s-1)*z for s,z,m in zip(self.shape, self.strides, mul) if m < 0])
    mask = tuple([(((mx if m > 0 else s-my)+(abs(m)-1))//abs(m), ((my if m > 0 else s-mx)+(abs(m)-1))//abs(m)) for (mx,my),s,m in zip(self.mask, self.shape, mul)]) if self.mask is not None else None
    return View.create(new_shape, strides, self.offset + offset, mask)

  @functools.lru_cache(maxsize=None)  # pylint: disable=method-cache-max-size-none
  def reshape(self, new_shape: Tuple[sint, ...]) -> Optional[View]:
    if self.shape == new_shape: return self

    assert all(is_sym_int(x) and x > 0 for x in new_shape), f"shape must be symbolic ints and can't contain 0 or negative numbers {new_shape}"
    # check for the same size
    if all_int(self.shape):
      if all_int(new_shape):
        assert prod(self.shape) == prod(new_shape), f"size mismatched, can't reshape {self.shape=} -> {new_shape=}"
      else:
        assert all(isinstance(s, (int, Variable)) for s in new_shape), f"{self.shape=} -> {new_shape=} contains non (int, Variable) dim"
        assert prod(self.shape) == prod([s if isinstance(s, int) else cast(Variable,s).val for s in new_shape]), f"size mismatched, can't reshape {self.shape=} -> {new_shape=}"

    # after the asserts, it's okay to check contiguous
    if self.contiguous: return View.create(new_shape)

    strides, reverse_shape, total_offset = [], reversed(new_shape), 0
    for d, s, off in reversed(to_shape_strides(self.shape, self.strides, self.mask)):
      acc, new_stride, equal = 1, s, False
      total_offset = total_offset * d - off
      while acc <= d and not equal:
        try: new_dim = next(reverse_shape)
        except StopIteration: break
        acc *= new_dim
        strides.append(new_stride)
        new_stride *= new_dim
        if acc == d: equal = True
      if not equal: break
    else:
      strides += [0,] * (len(new_shape) - len(strides))
      mask, extra = _reshape_mask(self, new_shape)
      if not extra: return View.create(new_shape, tuple(reversed(strides)), self.offset + total_offset, mask)

    return None

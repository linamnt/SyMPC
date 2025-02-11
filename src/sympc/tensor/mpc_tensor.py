"""Class used to orchestrate the computation on shared values."""

# stdlib
from functools import lru_cache
import operator
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
from typing import Union

# third party
from syft.core.node.common.client import Client
import torch
import torchcsprng as csprng  # type: ignore

from sympc.encoder import FixedPointEncoder
from sympc.session import Session
from sympc.tensor import ShareTensor
from sympc.utils import islocal
from sympc.utils import ispointer
from sympc.utils import parallel_execution

from .tensor import SyMPCTensor

PROPERTIES_FORWARD_ALL_SHARES = {"T"}
METHODS_FORWARD_ALL_SHARES = {}


class MPCTensor(metaclass=SyMPCTensor):
    """Used by the orchestrator to compute on the shares.

    Arguments:
        session (Session): the session
        secret (Optional[Union[torch.Tensor, float, int]): in case the secret is
            known by the orchestrator it is split in shares and given to multiple
            parties
        shape (Optional[Union[torch.Size, tuple]): the shape of the secret in case
            the secret is not known by the orchestrator
            this is needed when a multiplication is needed between two secret values
            (need the shapes to be able to generate random elements in the proper way)
        shares (Optional[List[ShareTensor]]): in case the shares are already at the
             parties involved in the computation

    This class is used by an orchestrator that wants to do computation on
    data it does not see.

    Attributes:
        share_ptrs (List[ShareTensor]): pointer to the shares (hold by the parties)
        session (Session): session used for the MPC
        shape (Union[torch.size, tuple]): the shape for the shared secret
    """

    __slots__ = {"share_ptrs", "session", "shape"}

    # Used by the SyMPCTensor metaclass
    METHODS_FORWARD = {"numel"}
    PROPERTIES_FORWARD = {"T"}

    def __init__(
        self,
        session: Optional[Session] = None,
        secret: Optional[Union[ShareTensor, torch.Tensor, float, int]] = None,
        shape: Optional[Union[torch.Size, List[int], Tuple[int, ...]]] = None,
        shares: Optional[List[ShareTensor]] = None,
    ) -> None:
        """Initializer for the MPCTensor. It can be used in two ways.

        ShareTensorControlCenter can be used in two ways:
        - secret is known by the orchestrator.
        - secret is not known by the orchestrator (PRZS is employed).

        Args:
            session (Optional[Session]): The session. Defaults to None.
            secret (Optional[Union[ShareTensor, torch.Tensor, float, int]]): In case the secret is
                known by the orchestrator it is split in shares and given to multiple
                parties. Defaults to None.
            shape (Optional[Union[torch.Size, List[int], Tuple[int, ...]]]): The shape of the
                secret in case the secret is not known by the orchestrator this is needed
                when a multiplication is needed between two secret values (need the shapes
                to be able to generate random elements in the proper way). Defaults to None
            shares (Optional[List[ShareTensor]]): In case the shares are already at the
                parties involved in the computation. Defaults to None

        Raises:
            ValueError: If session is not provided as argument or in the ShareTensor.
        """
        if session is None and (
            not isinstance(secret, ShareTensor) or secret.session is None
        ):
            raise ValueError(
                "Need to provide a session, as argument or the secret should be a ShareTensor"
            )

        self.session = session if session is not None else secret.session

        if len(self.session.session_ptrs) == 0:
            raise ValueError("setup_mpc was not called on the session")

        self.shape = None

        if secret is not None:
            """In the case the secret is hold by a remote party then we use the
            PRZS to generate the shares and then the pointer tensor is added to
            share specific to the holder of the secret."""
            secret, self.shape, is_remote_secret = MPCTensor.sanity_checks(
                secret, shape, self.session
            )

            if is_remote_secret:
                # If the secret is remote we use PRZS (Pseudo-Random-Zero Shares) and the
                # party that holds the secret will add it to its share
                self.share_ptrs = MPCTensor.generate_przs(
                    shape=self.shape, session=self.session
                )
                for i, share in enumerate(self.share_ptrs):
                    if share.client == secret.client:  # type: ignore
                        self.share_ptrs[i] = self.share_ptrs[i] + secret
                        return
            else:
                tensor_type = self.session.tensor_type
                shares = MPCTensor.generate_shares(
                    secret=secret,
                    nr_parties=self.session.nr_parties,
                    tensor_type=tensor_type,
                )

        if not ispointer(shares[0]):
            shares = MPCTensor.distribute_shares(shares, self.session.parties)

        if shape is not None:
            self.shape = shape

        self.share_ptrs = shares

    @staticmethod
    def distribute_shares(shares: List[ShareTensor], parties: List[Client]):
        """Distribute a list of shares.

        Args:
            shares (List[ShareTensor): list of shares to distribute.
            parties (List[Client]): list to parties to distribute.

        Returns:
            List of ShareTensorPointers.
        """
        share_ptrs = []
        for share, party in zip(shares, parties):
            share_ptrs.append(share.send(party))

        return share_ptrs

    @staticmethod
    def sanity_checks(
        secret: Union[ShareTensor, torch.Tensor, float, int],
        shape: Optional[Union[torch.Size, List[int], Tuple[int, ...]]],
        session: Session,
    ) -> Tuple[
        Union[ShareTensor, torch.Tensor, float, int],
        Union[torch.Size, List[int], Tuple[int, ...]],
        bool,
    ]:
        """Sanity check to validate that a new instance for MPCTensor can be created.

        Args:
            secret (Union[ShareTensor, torch.Tensor, float, int]): Secret to check.
            shape (Optional[Union[torch.Size, List[int], Tuple[int, ...]]]): shape of the secret.
                Mandatory if secret is at another party.
            session (Session): Session.

        Returns:
            Tuple representing the ShareTensor, the shape, boolean if the secret is remote or local.

        Raises:
            ValueError: If secret is at another party and shape is not specified.
        """
        is_remote_secret: bool = False

        if ispointer(secret):
            is_remote_secret = True
            if shape is None:
                raise ValueError(
                    "Shape must be specified if secret is at another party"
                )

            shape = shape
        else:
            if isinstance(secret, (int, float)):
                secret = torch.tensor(data=[secret])

            if isinstance(secret, torch.Tensor):
                secret = ShareTensor(data=secret, session=session)

            shape = secret.shape

        return secret, shape, is_remote_secret

    @staticmethod
    def generate_przs(
        shape: Union[torch.Size, List[int], Tuple[int, ...]],
        session: Session,
    ) -> List[ShareTensor]:
        """Generate Pseudo-Random-Zero Shares.

        PRZS at the parties involved in the computation.

        Args:
            shape (Union[torch.Size, List[int], Tuple[int, ...]]): Shape of the tensor.
            session (Session): Session.

        Returns:
            List[ShareTensor]. List of Pseudo-Random-Zero Shares.
        """
        shape = tuple(shape)

        shares = []
        for session_ptr, generators_ptr in zip(
            session.session_ptrs, session.przs_generators
        ):
            share_ptr = session_ptr.przs_generate_random_share(
                shape=shape, generators=generators_ptr
            )
            shares.append(share_ptr)

        return shares

    @staticmethod
    def generate_shares(
        secret: Union[ShareTensor, torch.Tensor, float, int],
        nr_parties: int,
        tensor_type: Optional[torch.dtype] = None,
        **kwargs,
    ) -> List[ShareTensor]:
        """Generate shares from secret.

        Given a secret, split it into a number of shares such that each
        party would get one.

        Args:
            secret (Union[ShareTensor, torch.Tensor, float, int]): Secret to split.
            nr_parties (int): Number of parties to split the scret.
            tensor_type (torch.dtype, optional): tensor type. Defaults to None.
            **kwargs: keywords arguments passed to ShareTensor.

        Returns:
            List[ShareTensor]. List of ShareTensor.

        Raises:
            ValueError: If secret is not a expected format.

        Examples:
            >>> from sympc.tensor.mpc_tensor import MPCTensor
            >>> MPCTensor.generate_shares(secret=2, nr_parties=2)
            [[ShareTensor]
                | [FixedPointEncoder]: precision: 16, base: 2
                | Data: tensor([15511500.]), [ShareTensor]
                | [FixedPointEncoder]: precision: 16, base: 2
                | Data: tensor([-15380428.])]
            >>> MPCTensor.generate_shares(secret=2, nr_parties=2,
                encoder_base=3, encoder_precision=4)
            [[ShareTensor]
                | [FixedPointEncoder]: precision: 4, base: 3
                | Data: tensor([14933283.]), [ShareTensor]
                | [FixedPointEncoder]: precision: 4, base: 3
                | Data: tensor([-14933121.])]
        """
        if isinstance(secret, (torch.Tensor, float, int)):
            secret = ShareTensor(secret, **kwargs)

        # if secret is not a ShareTensor, a new instance is created
        if not isinstance(secret, ShareTensor):
            raise ValueError(
                "Secret should be a ShareTensor, torchTensor, float or int."
            )

        op = operator.sub
        shape = secret.shape

        random_shares = []
        generator = csprng.create_random_device_generator()

        for _ in range(nr_parties - 1):
            rand_value = torch.empty(size=shape, dtype=tensor_type).random_(
                generator=generator
            )
            share = ShareTensor(session=secret.session)
            share.tensor = rand_value

            random_shares.append(share)

        shares = []
        for i in range(nr_parties):
            if i == 0:
                share = random_shares[i]
            elif i < nr_parties - 1:
                share = op(random_shares[i], random_shares[i - 1])
            else:
                share = op(secret, random_shares[i - 1])

            shares.append(share)
        return shares

    def reconstruct(
        self, decode: bool = True, get_shares: bool = False
    ) -> Union[torch.Tensor, List[torch.Tensor]]:
        """Reconstruct the secret.

        Request and get the shares from all the parties and reconstruct the
        secret. Depending on the value of "decode", the secret would be decoded
        or not using the FixedPrecision Encoder specific for the session.

        Args:
            decode (bool): True if decode using FixedPointEncoder. Defaults to True
            get_shares (bool): True if get shares. Defaults to False.

        Returns:
            torch.Tensor. The secret reconstructed.
        """

        def _request_and_get(share_ptr: ShareTensor) -> ShareTensor:
            """Function used to request and get a share - Duet Setup.

            Args:
                share_ptr (ShareTensor): a ShareTensor

            Returns:
                ShareTensor. The ShareTensor in local.

            """
            if not islocal(share_ptr):
                share_ptr.request(block=True)
            res = share_ptr.get_copy()
            return res

        request = _request_and_get
        request_wrap = parallel_execution(request)

        args = [[share] for share in self.share_ptrs]
        local_shares = request_wrap(args)

        shares = [share.tensor for share in local_shares]

        if get_shares:
            return shares

        plaintext = sum(shares)

        if decode:
            fp_encoder = FixedPointEncoder(
                base=self.session.config.encoder_base,
                precision=self.session.config.encoder_precision,
            )

            plaintext = fp_encoder.decode(plaintext)

        return plaintext

    get = reconstruct

    def get_shares(self):
        """Get the shares.

        Returns:
            List[MPCTensor]: List of shares.
        """
        res = self.reconstruct(get_shares=True)
        return res

    def add(self, y: Union["MPCTensor", torch.Tensor, float, int]) -> "MPCTensor":
        """Apply the "add" operation between "self" and "y".

        Args:
            y (Union["MPCTensor", torch.Tensor, float, int]): self + y

        Returns:
            MPCTensor. Result of the operation.
        """
        return self.__apply_op(y, "add")

    def sub(self, y: Union["MPCTensor", torch.Tensor, float, int]) -> "MPCTensor":
        """Apply the "sub" operation between "self" and "y".

        Args:
            y (Union["MPCTensor", torch.Tensor, float, int]): self - y

        Returns:
            MPCTensor. Result of the operation.
        """
        return self.__apply_op(y, "sub")

    def rsub(self, y: Union[torch.Tensor, float, int]) -> "MPCTensor":
        """Apply the "sub" operation between "y" and "self".

        Args:
            y (Union["MPCTensor", torch.Tensor, float, int]): self - y

        Returns:
            MPCTensor. Result of the operation.
        """
        return self.__apply_op(y, "sub") * -1

    def mul(self, y: Union["MPCTensor", torch.Tensor, float, int]) -> "MPCTensor":
        """Apply the "mul" operation between "self" and "y".

        Args:
            y (Union["MPCTensor", torch.Tensor, float, int]): self * y

        Returns:
            MPCTensor. Result of the operation.
        """
        return self.__apply_op(y, "mul")

    def matmul(self, y: Union["MPCTensor", torch.Tensor, float, int]) -> "MPCTensor":
        """Apply the "matmul" operation between "self" and "y".

        Args:
            y (Union["MPCTensor", torch.Tensor, float, int]): self @ y

        Returns:
            MPCTensor. Result of the operation.
        """
        return self.__apply_op(y, "matmul")

    def conv2d(
        self,
        weight: Union["MPCTensor", torch.Tensor, float, int],
        bias: Optional[torch.Tensor] = None,
        stride: int = 1,
        padding: int = 0,
        dilation: int = 1,
        groups: int = 1,
    ) -> "MPCTensor":
        """Apply the "conv2d" operation between "self" and "y".

        Args:
            weight: the convolution kernel
            bias: optional bias
            stride: stride
            padding: padding
            dilation: dilation
            groups: groups

        Returns:
            MPCTensor. Result of the operation.
        """
        kwargs = {
            "bias": bias,
            "stride": stride,
            "padding": padding,
            "dilation": dilation,
            "groups": groups,
        }

        bias = kwargs.pop("bias", None)

        convolution = self.__apply_op(weight, "conv2d", kwargs_=kwargs)

        if bias:
            return convolution + bias.unsqueeze(1).unsqueeze(1)
        else:
            return convolution

    def rmatmul(self, y: torch.Tensor) -> "MPCTensor":
        """Apply the "rmatmul" operation between "y" and "self".

        Args:
            y (Union["MPCTensor", torch.Tensor, float, int]): y @ self

        Returns:
            MPCTensor. Result of the operation.
        """
        op = getattr(operator, "matmul")
        shares = [op(y, share) for share in self.share_ptrs]

        if isinstance(y, (float, int)):
            y_shape = (1,)
        else:
            y_shape = y.shape

        result = MPCTensor(shares=shares, session=self.session)
        result.shape = MPCTensor._get_shape("matmul", y_shape, self.shape)

        scale = (
            self.session.config.encoder_base ** self.session.config.encoder_precision
        )
        result = result.div(scale)

        return result

    def div(self, y: Union["MPCTensor", torch.Tensor, float, int]) -> "MPCTensor":
        """Apply the "div" operation between "self" and "y".

        Args:
            y (Union["MPCTensor", torch.Tensor, float, int]): Denominator.

        Returns:
            MPCTensor: Result of the operation.

        Raises:
            NotImplementedError: If y is not a MPCTensor.
        """
        is_private = isinstance(y, MPCTensor)
        if is_private:
            raise NotImplementedError("Not implemented for MPCTensor")

        from sympc.protocol.spdz import spdz

        result = spdz.public_divide(self, y)
        return result

    def pow(self, power: int) -> "MPCTensor":
        """Compute integer power of a number by recursion using mul.

        - Divide power by 2 and multiply base to itself (if the power is even)
        - Decrement power by 1 to make it even and then follow the first step

        Args:
            power (int): integer value to apply the operation

        Returns:
             MPCTensor: Result of the pow operation

        Raises:
            RuntimeError: if negative power is given
        """
        if power < 0:
            raise RuntimeError("Negative integer powers are not allowed.")

        base = self

        result = 1
        while power > 0:
            # If power is odd
            if power % 2 == 1:
                result = result * base

            # Divide the power by 2
            power = power // 2
            # Multiply base to itself
            base = base * base

        return result

    def __apply_private_op(
        self, y: "MPCTensor", op_str: str, kwargs_: Dict[Any, Any]
    ) -> "MPCTensor":
        """Apply an operation on 2 MPCTensor (secret shared values).

        Args:
            y (MPCTensor): Tensor to apply the operation
            op_str (str): The operation
            kwargs_ (dict): Kwargs for some operations like conv2d

        Returns:
            MPCTensor. The operation "op_str" applied on "self" and "y"

        Raises:
            ValueError: If session from MPCTensor and "y" is not the same.
        """
        if y.session.uuid != self.session.uuid:
            raise ValueError(
                f"Need same session {self.session.uuid} and {y.session.uuid}"
            )

        if op_str in {"mul", "matmul", "conv2d"}:
            from sympc.protocol.spdz import spdz

            result = spdz.mul_master(self, y, op_str, kwargs_)
            result.shape = MPCTensor._get_shape(op_str, self.shape, y.shape)
        elif op_str in {"sub", "add"}:
            op = getattr(operator, op_str)
            shares = [
                op(*share_tuple) for share_tuple in zip(self.share_ptrs, y.share_ptrs)
            ]

            result = MPCTensor(shares=shares, shape=self.shape, session=self.session)

        return result

    def __apply_public_op(
        self, y: Union[torch.Tensor, float, int], op_str: str, kwargs_: Dict[Any, Any]
    ) -> "MPCTensor":
        """Apply an operation on "self" which is a MPCTensor and a public value.

        Args:
            y (Union[torch.Tensor, float, int]): Tensor to apply the operation.
            op_str (str): The operation.
            kwargs_ (dict): Kwargs for some operations like conv2d

        Returns:
            MPCTensor. The operation "op_str" applied on "self" and "y".

        Raises:
            ValueError: If "op_str" is not supported.
        """
        op = getattr(operator, op_str)
        if op_str in {"mul", "matmul"}:
            shares = [op(share, y) for share in self.share_ptrs]
        elif op_str in {"add", "sub"}:
            shares = list(self.share_ptrs)
            # Only the rank 0 party has to add the element
            shares[0] = op(shares[0], y)
        else:
            raise ValueError(f"{op_str} not supported")

        result = MPCTensor(shares=shares, session=self.session)
        return result

    @staticmethod
    @lru_cache(maxsize=128)
    def _get_shape(
        op_str: str, x_shape: Tuple[int], y_shape: Tuple[int], **kwargs_: Dict[Any, Any]
    ) -> Tuple[int]:

        if x_shape is None or y_shape is None:
            raise ValueError(
                f"Shapes should not be None; x_shape {x_shape}, y_shape {y_shape}"
            )

        if op_str == "conv2d":
            op = torch.conv2d
        else:
            op = getattr(operator, op_str)

        x = torch.empty(size=x_shape)
        y = torch.empty(size=y_shape)

        res = op(x, y, **kwargs_)
        return res.shape

    def __apply_op(
        self,
        y: Union["MPCTensor", torch.Tensor, float, int],
        op_str: str,
        kwargs_: Dict[Any, Any] = {},
    ) -> "MPCTensor":
        """Apply an operation on "self" which is a MPCTensor "y".

         This function checks if "y" is private or public value.

        Args:
            y: tensor to apply the operation.
            op_str: the operation.
            kwargs_ (dict): kwargs for some operations like conv2d

        Returns:
            MPCTensor. the operation "op_str" applied on "self" and "y"
        """
        is_private = isinstance(y, MPCTensor)

        if is_private:
            result = self.__apply_private_op(y, op_str, kwargs_)
        else:
            result = self.__apply_public_op(y, op_str, kwargs_)

        if isinstance(y, (float, int)):
            y_shape = (1,)
        else:
            y_shape = y.shape

        result.shape = MPCTensor._get_shape(op_str, self.shape, y_shape, **kwargs_)

        if op_str in {"mul", "matmul", "conv2d"} and not (
            is_private and self.session.nr_parties == 2
        ):
            # For private op we do the division in the mul_parties function from spdz
            scale = (
                self.session.config.encoder_base
                ** self.session.config.encoder_precision
            )
            result = result.div(scale)

        return result

    def __str__(self) -> str:
        """Return the string representation of MPCTensor.

        Returns:
            str: String representation.
        """
        type_name = type(self).__name__
        out = f"[{type_name}]\nShape: {self.shape}"

        for share in self.share_ptrs:
            out = f"{out}\n\t| {share.client} -> {share.__name__}"
        return out

    def __repr__(self):
        """Representation.

        Returns:
            str: Representation.
        """
        return self.__str__()

    @staticmethod
    def __check_or_convert(value, session) -> "MPCTensor":
        if not isinstance(value, MPCTensor):
            return MPCTensor(secret=value, session=session)
        else:
            return value

    @staticmethod
    def hook_property(property_name: str) -> Any:
        """Hook a framework property (only getter).

        Ex:
         * if we call "shape" we want to call it on the underlying share
        and return the result
         * if we call "T" we want to call it on all the underlying shares
        and wrap the result in an MPCTensor

        Args:
            property_name (str): property to hook

        Returns:
            A hooked property
        """

        def property_all_share_getter(_self: "MPCTensor") -> "MPCTensor":
            shares = []

            for share in _self.share_ptrs:
                prop = getattr(share, property_name)
                shares.append(prop)

            new_shape = getattr(torch.empty(_self.shape), property_name).shape
            res = MPCTensor(shares=shares, shape=new_shape, session=_self.session)
            return res

        def property_share_getter(_self: "MPCTensor") -> Any:
            prop = getattr(_self.share_ptrs[0], property_name)
            return prop

        if property_name in PROPERTIES_FORWARD_ALL_SHARES:
            res = property(property_all_share_getter, None)
        else:
            res = property(property_share_getter, None)

        return res

    @staticmethod
    def hook_method(method_name: str) -> Callable[..., Any]:
        """Hook a framework method.

        Ex:
         * if we call "numel" we want to forward it only to one share and return
        the result (without wrapping it in an MPCShare)
         * if we call "unsqueeze" we want to call it on all the underlying shares
        and we want to wrap those shares in a new MPCTensor

        Args:
            method_name (str): method to hook

        Returns:
            A hooked method
        """

        def method_all_shares(
            _self: "MPCTensor", *args: List[Any], **kwargs: Dict[Any, Any]
        ) -> Any:
            shares = []

            for share in _self.share_ptrs:
                method = getattr(share, method_name)
                shares.append(method(*args, **kwargs))

            new_shape = getattr(torch.empty(_self.shape), method_name).shape
            res = MPCTensor(shares=shares, shape=new_shape, session=_self.session)
            return res

        def method_share(
            _self: "MPCTensor", *args: List[Any], **kwargs: Dict[Any, Any]
        ) -> Any:
            method = getattr(_self.share_ptrs[0], method_name)
            res = method(*args, **kwargs)
            return res

        if method_name in METHODS_FORWARD_ALL_SHARES:
            res = method_all_shares
        else:
            res = method_share

        return res

    def unsqueeze(self, *args, **kwargs) -> "MPCTensor":
        """Tensor with a dimension of size one inserted at the specified position.

        Args:
            *args: Arguments to tensor.unsqueeze
            **kwargs: Keyword arguments passed to tensor.unsqueeze

        Returns:
            MPCTensor: Tensor unsqueezed.

        References:
            https://pytorch.org/docs/stable/generated/torch.unsqueeze.html
        """
        shares = [share.unsqueeze(*args, **kwargs) for share in self.share_ptrs]
        res = MPCTensor(shares=shares, session=self.session)
        res.shape = torch.empty(self.shape).unsqueeze(*args, **kwargs).shape
        return res

    def view(self, *args, **kwargs) -> "MPCTensor":
        """Tensor with the same data but new dimensions/view.

        Args:
            *args: Arguments to tensor.view.
            **kwargs: Keyword arguments passed to tensor.view.

        Returns:
            MPCTensor: Tensor with new view.

        References:
            https://pytorch.org/docs/stable/generated/torch.unsqueeze.html
        """
        shares = [share.view(*args, **kwargs) for share in self.share_ptrs]
        res = MPCTensor(shares=shares, session=self.session)
        res.shape = torch.empty(self.shape).view(*args, **kwargs).shape
        return res

    def le(self, other: "MPCTensor") -> "MPCTensor":
        """Lower or than operator.

        Args:
            other (MPCTensor): MPCTensor to compare.

        Returns:
            MPCTensor: Result of the comparison.
        """
        protocol = self.session.get_protocol()
        other = self.__check_or_convert(other, self.session)
        return protocol.le(self, other)

    def ge(self, other: "MPCTensor") -> "MPCTensor":
        """Greater or equal operator.

        Args:
            other (MPCTensor): MPCTensor to compare.

        Returns:
            MPCTensor: Result of the comparison.
        """
        protocol = self.session.get_protocol()
        other = self.__check_or_convert(other, self.session)
        return protocol.le(other, self)

    def lt(self, other: "MPCTensor") -> "MPCTensor":
        """Lower than operator.

        Args:
            other (MPCTensor): MPCTensor to compare.

        Returns:
            MPCTensor: Result of the comparison.
        """
        protocol = self.session.get_protocol()
        other = self.__check_or_convert(other, self.session)
        fp_encoder = FixedPointEncoder(
            base=self.session.config.encoder_base,
            precision=self.session.config.encoder_precision,
        )
        one = fp_encoder.decode(1)
        return protocol.le(self + one, other)

    def gt(self, other: "MPCTensor") -> "MPCTensor":
        """Greater than operator.

        Args:
            other (MPCTensor): MPCTensor to compare.

        Returns:
            MPCTensor: Result of the comparison.
        """
        protocol = self.session.get_protocol()
        other = self.__check_or_convert(other, self.session)
        fp_encoder = FixedPointEncoder(
            base=self.session.config.encoder_base,
            precision=self.session.config.encoder_precision,
        )
        one = fp_encoder.decode(1)
        r = other + one
        return protocol.le(r, self)

    def eq(self, other: "MPCTensor") -> "MPCTensor":
        """Equal operator.

        Args:
            other (MPCTensor): MPCTensor to compare.

        Returns:
            MPCTensor: Result of the comparison.
        """
        protocol = self.session.get_protocol()
        other = self.__check_or_convert(other, self.session)
        return protocol.eq(self, other)

    def ne(self, other: "MPCTensor") -> "MPCTensor":
        """Not equal operator.

        Args:
            other (MPCTensor): MPCTensor to compare.

        Returns:
            MPCTensor: Result of the comparison.
        """
        other = self.__check_or_convert(other, self.session)
        return 1 - self.eq(other)

    __add__ = add
    __radd__ = add
    __sub__ = sub
    __rsub__ = rsub
    __mul__ = mul
    __rmul__ = mul
    __matmul__ = matmul
    __rmatmul__ = rmatmul
    __truediv__ = div
    __pow__ = pow
    __le__ = le
    __ge__ = ge
    __lt__ = lt
    __gt__ = gt
    __eq__ = eq
    __ne__ = ne


PARTIES_TO_SESSION: Dict[Any, Session] = {}


def share(_self, **kwargs: Dict[Any, Any]) -> MPCTensor:  # noqa
    session = None

    if "parties" not in kwargs and "session" not in kwargs:
        raise ValueError("Parties or Session should be provided as a kwarg")

    if "session" not in kwargs:
        parties = frozenset({client.id for client in kwargs["parties"]})

        if parties not in PARTIES_TO_SESSION:
            from sympc.session import SessionManager

            session = Session(kwargs["parties"])
            PARTIES_TO_SESSION[parties] = session
            SessionManager.setup_mpc(session)

            for key, val in kwargs.items():
                setattr(session, key, val)
        else:
            session = PARTIES_TO_SESSION[parties]

        kwargs.pop("parties")
        kwargs["session"] = session

    return MPCTensor(secret=_self, **kwargs)


METHODS_TO_ADD = [share]

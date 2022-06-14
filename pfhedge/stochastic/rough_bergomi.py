from typing import Optional
from typing import Tuple

import torch

from pfhedge._utils.typing import TensorOrScalar
from pfhedge.stochastic._utils import cast_state
from pfhedge.stochastic.heston import SpotVarianceTuple


def generate_rough_bergomi(
    n_paths: int,
    n_steps: int,
    init_state: Optional[Tuple[TensorOrScalar, ...]] = None,
    alpha: float = -0.4,
    rho: float = -0.9,
    eta: float = 1.9,
    xi: float = 0.04,
    dt: float = 1 / 250,
    dtype: Optional[torch.dtype] = None,
    device: Optional[torch.device] = None,
) -> SpotVarianceTuple:
    r"""Returns time series following the rough Bergomi (rBergomi) model.

    The time evolution of the process is given by:

    .. math::

        S(t) &=& \exp{\left\{\int_0^t \sqrt{V(u)} dB(u) - \frac{1}{2} \int_0^t V(u) du \right\}}, \\
        B(u) &=& \rho W_u^1 + \sqrt{1-\rho^2}W_u^2, \\
        V(t) &=& \xi\exp{\left\{\eta Y^\alpha(t) - \frac{\eta^2}{2} t^{2\alpha + 1}\right\}}, \\
        Y^\alpha(t) &=& \sqrt{1\alpha + 1}\int_0^t (t-u)^\alpha dW_u^1,

    :math:`dW^1` and :math:`dW^2` are the Brownian motion.

    Time-series is generated by Ryan et al.'s Monte Carlo algorithm.

    References:
        - Bayer, C., Friz, P., & Gatheral, J. (2015). Pricing under rough volatility.
          Quantitative Finance, 16(6), 887–904. https://doi.org/10.1080/14697688.2015.1099717
        - McCrickerd, R., & Pakkanen, M. S. (2018). Turbocharging Monte Carlo pricing for the rough Bergomi model.
          Quantitative Finance, 18(11), 1877–1886. https://doi.org/10.1080/14697688.2018.1459812
          Code: https://github.com/ryanmccrickerd/rough_bergomi

    Args:
        n_paths (int): The number of simulated paths.
        n_steps (int): The number of time steps.
        init_state (tuple[torch.Tensor | float], optional): The initial state of
            the time series.
            This is specified by a tuple :math:`(S(0), V(0))`.
            If ``None`` (default), it uses :math:`(1.0, \\xi)`.
        alpha (float, default=-0.4): The parameter :math:`\\alpha`.
        rho (float, default=-0.9): The parameter :math:`\\rho`.
        eta (float, default=1.9): The parameter :math:`\\eta`.
        xi (float, default=0.04): The parameter :math:`\\xi`.
        dt (float, default=1 / 250): The intervals of the time steps.
        dtype (torch.dtype, optional): The desired data type of returned tensor.
            Default: If ``None``, uses a global default
            (see :func:`torch.set_default_tensor_type()`).
        device (torch.device, optional): The desired device of returned tensor.
            Default: If ``None``, uses the current device for the default tensor type
            (see :func:`torch.set_default_tensor_type()`).
            ``device`` will be the CPU for CPU tensor types and the current CUDA device
            for CUDA tensor types.

    Shape:
        - spot: :math:`(N, T)` where
          :math:`N` is the number of paths and
          :math:`T` is the number of time steps.
        - variance: :math:`(N, T)`.

    Returns:
        (torch.Tensor, torch.Tensor): A namedtuple ``(spot, variance)``.

    Examples:
        >>> from pfhedge.stochastic import generate_rough_bergomi
        ...
        >>> _ = torch.manual_seed(42)
        >>> outputs = generate_rough_bergomi(2, 5)
        >>> outputs.spot
        tensor([[1.0000, 0.9807, 0.9563, 0.9540, 0.9570],
                [1.0000, 1.0147, 1.0097, 1.0107, 1.0164]])
        >>> outputs.variance
        tensor([[0.0400, 0.3130, 0.0105, 0.0164, 0.0068],
                [0.0400, 0.0396, 0.0049, 0.0064, 0.0149]])

    """

    if init_state is None:
        init_state = (1.0, xi)

    init_state = cast_state(init_state, dtype=dtype, device=device)
    alpha_tensor, rho_tensor, eta_tensor = cast_state(
        (alpha, rho, eta), dtype=dtype, device=device
    )

    _dW1_cov1 = dt ** (alpha + 1) / (alpha + 1)
    _dW1_cov2 = dt ** (2 * alpha + 1) / (2 * alpha + 1)
    _dW1_covariance_matrix = torch.as_tensor(
        [[dt, _dW1_cov1], [_dW1_cov1, _dW1_cov2]], dtype=dtype, device=device
    )
    _dW1_generator = torch.distributions.multivariate_normal.MultivariateNormal(
        loc=torch.as_tensor([0.0, 0.0], dtype=dtype, device=device),
        covariance_matrix=_dW1_covariance_matrix,
    )

    dW1 = _dW1_generator.sample([n_paths, n_steps - 1])
    dW2 = torch.randn([n_paths, n_steps - 1], dtype=dtype, device=device)
    dW2 *= dW2.new_tensor(dt).sqrt()

    _Y1 = torch.cat(
        [torch.zeros([n_paths, 1], dtype=dtype, device=device), dW1[:, :, 1]], dim=-1
    )
    discrete_TBSS_fn = lambda k, a: ((k ** (a + 1) - (k - 1) ** (a + 1)) / (a + 1)) ** (
        1 / a
    )
    _gamma = (
        discrete_TBSS_fn(torch.arange(2, n_steps, dtype=dtype, device=device), alpha)
        / (n_steps - 1)
    ) ** alpha
    _gamma = torch.cat([torch.zeros([2], dtype=dtype, device=device), _gamma], dim=0)
    _Xi = dW1[:, :, 0]
    _GXi_convolve = torch.nn.functional.conv1d(
        _gamma.__reversed__().repeat(1, 1, 1),
        _Xi.unsqueeze(dim=1),
        padding=_Xi.size(1) - 1,
    ).squeeze(dim=0)
    _Y2 = _GXi_convolve[:, torch.arange(-1, -1 - n_steps, -1)]
    Y = torch.sqrt(2 * alpha_tensor + 1) * (_Y1 + _Y2)
    dB = rho_tensor * dW1[:, :, 0] + torch.sqrt(1 - rho_tensor.square()) * dW2
    variance = init_state[1] * torch.exp(
        eta_tensor * Y
        - 0.5
        * eta_tensor.square()
        * (torch.arange(0, n_steps, dtype=dtype, device=device) * dt)
        ** (2 * alpha_tensor + 1)
    )

    _increments = variance[:, :-1].sqrt() * dB - 0.5 * variance[:, :-1] * dt
    _integral = torch.cumsum(_increments, dim=1)
    log_return = torch.cat(
        [torch.zeros((n_paths, 1), dtype=dtype, device=device), _integral], dim=-1
    )
    prices = init_state[0] * log_return.exp()

    return SpotVarianceTuple(prices, variance)

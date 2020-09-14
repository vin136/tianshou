import torch
import numpy as np
from torch.distributions import Categorical
from typing import Any, Dict, Tuple, Union, Optional

from tianshou.policy import SACPolicy
from tianshou.data import Batch, ReplayBuffer, to_torch


class DiscreteSACPolicy(SACPolicy):
    """Implementation of SAC for Discrete Action Settings. arXiv:1910.07207.

    :param torch.nn.Module actor: the actor network following the rules in
        :class:`~tianshou.policy.BasePolicy`. (s -> logits)
    :param torch.optim.Optimizer actor_optim: the optimizer for actor network.
    :param torch.nn.Module critic1: the first critic network. (s -> Q(s))
    :param torch.optim.Optimizer critic1_optim: the optimizer for the first
        critic network.
    :param torch.nn.Module critic2: the second critic network. (s -> Q(s))
    :param torch.optim.Optimizer critic2_optim: the optimizer for the second
        critic network.
    :param float tau: param for soft update of the target network, defaults to
        0.005.
    :param float gamma: discount factor, in [0, 1], defaults to 0.99.
    :param (float, torch.Tensor, torch.optim.Optimizer) or float alpha: entropy
        regularization coefficient, default to 0.2.
        If a tuple (target_entropy, log_alpha, alpha_optim) is provided, then
        alpha is automatatically tuned.
    :param bool reward_normalization: normalize the reward to Normal(0, 1),
        defaults to ``False``.
    :param bool ignore_done: ignore the done flag while training the policy,
        defaults to ``False``.

    .. seealso::

        Please refer to :class:`~tianshou.policy.BasePolicy` for more detailed
        explanation.
    """

    def __init__(
        self,
        actor: torch.nn.Module,
        actor_optim: torch.optim.Optimizer,
        critic1: torch.nn.Module,
        critic1_optim: torch.optim.Optimizer,
        critic2: torch.nn.Module,
        critic2_optim: torch.optim.Optimizer,
        tau: float = 0.005,
        gamma: float = 0.99,
        alpha: Union[
            float, Tuple[float, torch.Tensor, torch.optim.Optimizer]
        ] = 0.2,
        reward_normalization: bool = False,
        ignore_done: bool = False,
        estimation_step: int = 1,
        **kwargs: Any,
    ) -> None:
        super().__init__(actor, actor_optim, critic1, critic1_optim, critic2,
                         critic2_optim, (-np.inf, np.inf), tau, gamma, alpha,
                         reward_normalization, ignore_done, estimation_step,
                         **kwargs)
        self._alpha: Union[float, torch.Tensor]

    def forward(  # type: ignore
        self,
        batch: Batch,
        state: Optional[Union[dict, Batch, np.ndarray]] = None,
        input: str = "obs",
        **kwargs: Any,
    ) -> Batch:
        obs = batch[input]
        logits, h = self.actor(obs, state=state, info=batch.info)
        dist = Categorical(logits=logits)
        act = dist.sample()
        return Batch(logits=logits, act=act, state=h, dist=dist)

    def _target_q(
        self, buffer: ReplayBuffer, indice: np.ndarray
    ) -> torch.Tensor:
        batch = buffer[indice]  # batch.obs: s_{t+n}
        with torch.no_grad():
            obs_next_result = self(batch, input="obs_next")
            dist = obs_next_result.dist
            target_q = dist.probs * torch.min(
                self.critic1_old(batch.obs_next),
                self.critic2_old(batch.obs_next),
            )
            target_q = target_q.sum(dim=-1) + self._alpha * dist.entropy()
        return target_q

    def learn(self, batch: Batch, **kwargs: Any) -> Dict[str, float]:
        weight = batch.pop("weight", 1.0)
        target_q = batch.returns.flatten()
        act = to_torch(
            batch.act[:, np.newaxis], device=target_q.device, dtype=torch.long)

        # critic 1
        current_q1 = self.critic1(batch.obs).gather(1, act).flatten()
        td1 = current_q1 - target_q
        critic1_loss = (td1.pow(2) * weight).mean()

        self.critic1_optim.zero_grad()
        critic1_loss.backward()
        self.critic1_optim.step()

        # critic 2
        current_q2 = self.critic2(batch.obs).gather(1, act).flatten()
        td2 = current_q2 - target_q
        critic2_loss = (td2.pow(2) * weight).mean()

        self.critic2_optim.zero_grad()
        critic2_loss.backward()
        self.critic2_optim.step()
        batch.weight = (td1 + td2) / 2.0  # prio-buffer

        # actor
        dist = self(batch).dist
        entropy = dist.entropy()
        with torch.no_grad():
            current_q1a = self.critic1(batch.obs)
            current_q2a = self.critic2(batch.obs)
            q = torch.min(current_q1a, current_q2a)
        actor_loss = -(self._alpha * entropy
                       + (dist.probs * q).sum(dim=-1)).mean()
        self.actor_optim.zero_grad()
        actor_loss.backward()
        self.actor_optim.step()

        if self._is_auto_alpha:
            log_prob = entropy.detach() - self._target_entropy
            alpha_loss = (self._log_alpha * log_prob).mean()
            self._alpha_optim.zero_grad()
            alpha_loss.backward()
            self._alpha_optim.step()
            self._alpha = self._log_alpha.detach().exp()

        self.sync_weight()

        result = {
            "loss/actor": actor_loss.item(),
            "loss/critic1": critic1_loss.item(),
            "loss/critic2": critic2_loss.item(),
        }
        if self._is_auto_alpha:
            result["loss/alpha"] = alpha_loss.item()
            result["alpha"] = self._alpha.item()  # type: ignore

        return result

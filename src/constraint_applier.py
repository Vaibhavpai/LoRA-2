"""
constraint_applier.py — Phase 6 (Hybrid v3)
===========================================
Applies soft weight-space projection to ΔW=(B@A)*scaling at each evaluation step.

This replaces the previous gradient hook mechanism. By operating on ΔW directly,
we correctly constrain the exact quantity the subspace alignment metric measures,
preventing drift via lora_B which was previously unconstrained.

Soft projection formula (generalizes baselines.project_lora_layer):
    ΔW_safe = ΔW - lambda_l * (ΔW @ P_l)
    P_l = U_l @ U_l.T

lambda_l = 1.0 reproduces SafeLoRA-B exactly for that layer.
lambda_l = 0.0 leaves that layer fully untouched (vanilla).
"""

import logging
import torch

logger = logging.getLogger(__name__)

class ConstraintApplier:
    """
    Holds per-projection lambda + projection matrices, and applies soft
    weight-space projection to ΔW=(B@A)*scaling on demand (call apply_projection()).
    """

    def __init__(self, model, safety_directions: dict, device: str, initial_lambda: float = 0.0):
        self.model = model
        self.device = device

        self.module_keys = list(safety_directions.keys())
        self.lambdas = {k: initial_lambda for k in self.module_keys}

        self.proj_matrices = {}
        for key, U_l in safety_directions.items():
            U = U_l.to(torch.float32).to(device)
            P_l = (U @ U.T).detach().requires_grad_(False)
            self.proj_matrices[key] = P_l

        logger.info(f"ConstraintApplier (weight-space) initialized for {len(self.module_keys)} modules.")

    def set_lambda(self, key: str, value: float):
        if key in self.lambdas:
            self.lambdas[key] = max(0.0, min(1.0, value))

    def set_all_lambdas(self, lambda_dict: dict):
        # Allow nested dicts e.g. {"layer_0": {"q_proj": 0.1}} or flat dicts
        for key, value in lambda_dict.items():
            if isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    # Attempt to match string
                    for actual_key in self.module_keys:
                        if key in actual_key and sub_key in actual_key:
                            self.set_lambda(actual_key, float(sub_value))
            else:
                if key in self.lambdas:
                    self.set_lambda(key, float(value))

    def get_lambdas(self) -> dict:
        return dict(self.lambdas)

    def _project_one_module(self, module_name: str, P: torch.Tensor, lam: float) -> bool:
        """Soft version of Weight-Space projection, scaled by lam."""
        if lam <= 0.0:
            return False  # no-op, leave weights untouched

        target_module = None
        for name, mod in self.model.named_modules():
            if module_name in name and hasattr(mod, "lora_A"):
                target_module = mod
                break
        
        if target_module is None:
            return False

        try:
            lora_A = target_module.lora_A["default"].weight
            lora_B = target_module.lora_B["default"].weight
            scaling = target_module.scaling.get("default", 1.0)
        except Exception:
            return False

        r = lora_A.shape[0]
        orig_dtype = lora_A.dtype
        dev = lora_A.device
        P_dev = P.to(dev)

        with torch.no_grad():
            A = lora_A.detach().to(torch.float32)
            B = lora_B.detach().to(torch.float32)

            delta_W = (B @ A) * scaling
            delta_W_safe = delta_W - lam * (delta_W @ P_dev)  # SOFT projection, scaled by lambda

            # Factor back into A and B
            target_W = delta_W_safe / scaling

            U_svd, S_svd, Vh_svd = torch.linalg.svd(target_W, full_matrices=False)
            U_r = U_svd[:, :r]
            S_r = S_svd[:r]
            Vh_r = Vh_svd[:r, :]

            sqrt_S = torch.sqrt(S_r.clamp(min=0.0))
            new_B = U_r * sqrt_S.unsqueeze(0)
            new_A = sqrt_S.unsqueeze(1) * Vh_r

            lora_B.data.copy_(new_B.to(orig_dtype))
            lora_A.data.copy_(new_A.to(orig_dtype))

        return True

    def apply_projection(self) -> int:
        """
        Call this AFTER agent.decide() + set_all_lambdas(), every eval_every steps.
        """
        n_applied = 0
        for key in self.module_keys:
            lam = self.lambdas.get(key, 0.0)
            P = self.proj_matrices[key]
            if self._project_one_module(key, P, lam):
                n_applied += 1
        logger.info(f"Weight-space projection applied to {n_applied} modules.")
        return n_applied
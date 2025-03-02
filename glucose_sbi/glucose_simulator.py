import logging
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import torch
from pathos.multiprocessing import ProcessingPool as Pool
from simglucose.actuator.pump import InsulinPump
from simglucose.controller.basal_bolus_ctrller import BBController
from simglucose.patient.t1dpatient import T1DPatient
from simglucose.sensor.cgm import CGMSensor
from simglucose.simulation.env import T1DSimEnv
from simglucose.simulation.scenario import CustomScenario
from simglucose.simulation.sim_engine import SimObj
from tqdm import tqdm

from glucose_sbi.prepare_priors import InferredParams

pathos = True


@dataclass
class DeafultSimulationEnv:
    """Dataclass for the default simulation environment."""

    patient_name: str
    sensor_name: str
    pump_name: str
    scenario: list[tuple[int, int]] = field(default_factory=list)
    hours: int = 24  # hours to simulate


def run_glucose_simulator(
    theta: torch.Tensor,
    default_settings: DeafultSimulationEnv,
    inferred_params: InferredParams,
    hours: int = 24,
    *,
    device: torch.device,
    infer_meal_params: bool = False,
    logger: logging.Logger | None = None,
) -> torch.Tensor:
    """Run the glucose simulator for a batch of custom parameters.

    Parameters
    ----------
    theta : torch.Tensor
        Sets of custom parameters to use for the simulation of shape (N_sets, N_params)
    default_settings : DeafultSimulationEnv
        DataClass object containing the default simulation environment settings.
    inferred_params : InferredParams
        DataClass object containing the names of inferred parameters
    hours : int, optional
        Duration of the simulation, by default 24
    device : torch.device, optional
        Device used to store the results, by default torch.device("cpu")
    logger : logging.Logger, optional
        The logger object, by default None
    infer_meal_params : bool, optional
        Whether to infer meal parameters, by default False

    Returns
    -------
    torch.Tensor
        The glucose dynamics time series for each simulation

    """
    if logger:
        logger.info("Running the glucose simulator on theta of shape, %s", theta.shape)
    simulation_envs = create_simulation_envs_with_custom_params(
        theta=theta,
        default_settings=default_settings,
        inferred_params=inferred_params,
        infer_meal_params=infer_meal_params,
        hours=hours,
    )
    return simulate_batch(simulation_envs, device, logger)


def simulate_batch(
    simulations: list[T1DSimEnv],
    device: torch.device,
    logger: logging.Logger | None = None,
) -> torch.Tensor:
    """Simulate a batch of simulation environments in parallel.

    Parameters
    ----------
    simulations : list[T1DSimEnv]
        List of simulation environments
    device : torch.device
        The device to store the results on, by default torch.device("cpu")
    logger : logging.Logger, optional
        The logger object, by default None

    Returns
    -------
    torch.Tensor
        The glucose dynamics for each simulation

    """
    pathos = True
    tic = time.time()
    if pathos:
        if logger:
            logger.info("Using pathos for parallel processing")
        with Pool() as p:
            results = p.map(simulate_glucose_dynamics, simulations)
    else:
        results = [simulate_glucose_dynamics(s) for s in tqdm(simulations)]
    results = np.stack(results)
    toc = time.time()
    if logger:
        # log in seconds
        logger.info("Simulation took %s seconds", toc - tic)
    return torch.from_numpy(results).float().to(device)


def simulate_glucose_dynamics(simulation_env: T1DSimEnv) -> np.ndarray:
    """Simulates the glucose dynamics for a given simulation environment.

    Parameters
    ----------
    simulation_env : T1DSimEnv
        The simulation environment object

    Returns
    -------
    np.ndarray
        The glucose dynamics

    """
    simulation_env.simulate()
    return simulation_env.results()["CGM"].to_numpy()


def create_simulation_envs_with_custom_params(
    theta: torch.Tensor,
    default_settings: DeafultSimulationEnv,
    inferred_params: InferredParams,
    hours: int = 24,
    *,
    infer_meal_params: bool = False,
) -> list[T1DSimEnv]:
    """Creates a list of simulation environments with custom parameters.

    Parameters
    ----------
    theta : torch.Tensor
        Sets of custom parameters to use for the simulation of shape (N_sets, N_params)
    default_settings : DeafultSimulationEnv
        DataClass object containing the default simulation environment settings.
    inferred_params : InferredParams
        DataClass object containing the names of inferred parameters
    hours : int, optional
        Duration of simulation, by default 24
    infer_meal_params : bool, optional
        Whether to infer meal parameters, by default False

    Returns
    -------
    list[T1DSimEnv]
        List of simulation environments with custom parameters

    """
    default_simulation_env = load_default_simulation_env(
        hours=hours, env_settings=default_settings
    )
    simulation_envs = []
    for _, theta_i in enumerate(theta):
        custom_sim_env = deepcopy(default_simulation_env)

        set_custom_params(
            custom_sim_env,
            theta_i,
            inferred_params,
            infer_meal_params=infer_meal_params,
        )
        simulation_envs.append(custom_sim_env)

    return simulation_envs


def set_custom_params(
    default_simulation_env: T1DSimEnv,
    theta: torch.Tensor,
    inferred_params: InferredParams,
    *,
    infer_meal_params: bool = False,
) -> None:
    """Apply the custom parameters (used for a particular simulation) for the patient.

    Parameters
    ----------
    default_simulation_env : DefaultSimulationEnv
        The simulation environment containing the patient and scenario.
    theta : torch.Tensor
        One set of custom parameters to apply to the patient.
    inferred_params : InferredParams
        DataClass object containing the names of inferred parameters.
    infer_meal_params : bool, optional
        Whether to infer meal parameters, by default False

    """
    theta_list = theta.clone().tolist()
    param_names = inferred_params.params_names
    patient = default_simulation_env.env.patient

    # Separate meal and non-meal parameters
    meal_indices, meal_values, other_params, other_values = _separate_parameters(
        param_names, theta_list
    )

    if infer_meal_params and meal_indices:
        # Update meal parameters in the scenario
        _update_meal_parameters(
            default_simulation_env.env.scenario.scenario, meal_values
        )

    # Update other parameters in the patient
    _update_patient_parameters(patient, other_params, other_values)


def _separate_parameters(
    param_names: list[str], theta_list: list[float]
) -> tuple[list[int], list[float], list[str], list[float]]:
    """Separate meal and non-meal parameters."""
    meal_indices = [i for i, param in enumerate(param_names) if "meal" in param]
    meal_values = [theta_list[i] for i in meal_indices]

    non_meal_indices_and_params = [
        (i, param) for i, param in enumerate(param_names) if "meal" not in param
    ]
    other_params = [param for _, param in non_meal_indices_and_params]
    other_values = [theta_list[i] for i, _ in non_meal_indices_and_params]

    return meal_indices, meal_values, other_params, other_values


def _update_meal_parameters(
    scenario: list[tuple[str, float]], meal_values: list[float]
) -> None:
    """Update meal parameters in the scenario."""
    for i, (meal_name, _) in enumerate(scenario):
        scenario[i] = (meal_name, meal_values[i])


def _update_patient_parameters(
    patient: T1DPatient, params: list[str], values: list[float]
) -> None:
    """Update non-meal parameters in the patient."""
    for param, value in zip(params, values):
        setattr(patient._params, param, value)  # noqa: SLF001


def load_default_simulation_env(
    env_settings: DeafultSimulationEnv, hours: int = 24
) -> T1DSimEnv:
    """Load the default simulation environment.

    Parameters
    ----------
    env_settings : DeafultSimulationEnv
        DataClass object containing the default simulation environment settings.
    hours : int, optional
        The number of hours to simulate, by default 24

    Returns
    -------
    T1DSimEnv
        The simulation environment object.

    """
    now = datetime.now(tz=timezone.utc)
    start_time = datetime.combine(now.date(), datetime.min.time())

    patient = T1DPatient.withName(env_settings.patient_name)
    sensor = CGMSensor.withName(env_settings.sensor_name, seed=1)
    pump = InsulinPump.withName(env_settings.pump_name)
    scenario = CustomScenario(start_time=start_time, scenario=env_settings.scenario)
    controller = BBController()
    env = T1DSimEnv(patient=patient, sensor=sensor, pump=pump, scenario=scenario)

    return SimObj(
        env=env, controller=controller, sim_time=timedelta(hours=hours), animate=False
    )

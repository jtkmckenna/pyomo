#  _________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright (c) 2014 Sandia Corporation.
#  Under the terms of Contract DE-AC04-94AL85000 with Sandia Corporation,
#  the U.S. Government retains certain rights in this software.
#  This software is distributed under the BSD License.
#  _________________________________________________________________________

__all__ = ("ScenarioTreeSolverWorker",)

import time

from pyomo.opt import SolverFactory
from pyomo.pysp.util.configured_object import PySPConfiguredObject
from pyomo.pysp.util.config import (PySPConfigBlock,
                                    safe_register_common_option)
from pyomo.pysp.scenariotree.scenariotreeworkerbasic import \
    ScenarioTreeWorkerBasic
from pyomo.pysp.scenariotree.scenariotreesolvermanager import \
    (_ScenarioTreeSolverWorkerImpl,
     _ScenarioTreeSolverManager)
from six import iteritems

class ScenarioTreeSolverWorker(_ScenarioTreeSolverWorkerImpl,
                               _ScenarioTreeSolverManager,
                               ScenarioTreeWorkerBasic,
                               PySPConfiguredObject):

    _registered_options = \
        PySPConfigBlock("Options registered for the ScenarioTreeWorkerSolver class")

    def __init__(self, *args, **kwds):

        super(ScenarioTreeSolverWorker, self).__init__(*args, **kwds)
        # Maps ScenarioTreeID's on the master node ScenarioTree to
        # ScenarioTreeID's on this ScenarioTreeWorkers's ScenarioTree
        # (by node name)
        self._master_scenario_tree_id_map = {}
        self._reverse_master_scenario_tree_id_map = {}

        for scenario in self._scenario_tree._scenarios:
            assert scenario._instance is not None
            solver = self._scenario_solvers[scenario.name] = \
                SolverFactory(self._options.solver,
                              solver_io=self._options.solver_io)
            if self._preprocessor is not None:
                self._preprocessor.add_scenario(scenario,
                                                scenario._instance,
                                                solver)
        for bundle in self._scenario_tree._scenario_bundles:
            solver = self._bundle_solvers[bundle.name] = \
                SolverFactory(self._options.solver,
                              solver_io=self._options.solver_io)
            bundle_instance = \
                self._bundle_binding_instance_map[bundle.name]
            if self._preprocessor is not None:
                self._preprocessor.add_bundle(bundle,
                                              bundle_instance,
                                              solver)

    #
    # Update the map from local to master scenario tree ids
    #

    def _update_master_scenario_tree_ids_for_client(self, object_name, new_ids):

        if self._options.verbose:
            if self._scenario_tree.contains_bundles():
                print("Received request to update master "
                      "scenario tree ids for bundle="+object_name)
            else:
                print("Received request to update master "
                      "scenario tree ids scenario="+object_name)

        for node_name, new_master_node_ids in iteritems(new_ids):
            tree_node = self._scenario_tree.get_node(node_name)
            name_index_to_id = tree_node._name_index_to_id

            self._master_scenario_tree_id_map[tree_node.name] = \
                dict((master_variable_id, name_index_to_id[name_index])
                     for master_variable_id, name_index
                     in iteritems(new_master_node_ids))

            self._reverse_master_scenario_tree_id_map[tree_node.name] = \
                dict((local_variable_id, master_variable_id)
                     for master_variable_id, local_variable_id
                     in iteritems(self._master_scenario_tree_id_map\
                                  [tree_node.name]))

    def _collect_scenario_tree_data_for_client(self, tree_object_names):

        data = {}
        node_data = data['nodes'] = {}
        for node_name in tree_object_names['nodes']:
            tree_node = self._scenario_tree.get_node(node_name)
            this_node_data = node_data[node_name] = {}
            this_node_data['_variable_ids'] = tree_node._variable_ids
            this_node_data['_standard_variable_ids'] = \
                tree_node._standard_variable_ids
            this_node_data['_variable_indices'] = tree_node._variable_indices
            this_node_data['_discrete'] = list(tree_node._discrete)
            # master will need to reconstruct
            # _derived_variable_ids
            # _name_index_to_id

        scenario_data = data['scenarios'] = {}
        for scenario_name in tree_object_names['scenarios']:
            scenario = self._scenario_tree.get_scenario(scenario_name)
            this_scenario_data = scenario_data[scenario_name] = {}
            this_scenario_data['_objective_name'] = scenario._objective_name
            this_scenario_data['_objective_sense'] = scenario._objective_sense

        return data

    # TODO: functionality for returning suffixes and
    #       functionality to limit variable return values to
    #       non-leaf stages
    def _solve_objects_for_client(self,
                                  object_type,
                                  objects,
                                  ephemeral_solver_options,
                                  disable_warmstart):

        if self._options.verbose:
            print("Received request to queue solves for %s" % (object_type))

        failures = super(ScenarioTreeSolverWorker, self).\
                   _solve_objects(object_type,
                                  objects,
                                  ephemeral_solver_options,
                                  disable_warmstart,
                                  False, # exception_on_failure
                                  True,  # process_results
                                  False) # async

        if object_type == 'bundles':
            if objects is None:
                objects = self._scenario_tree._scenario_bundle_map
        else:
            assert object_type == 'scenarios'
            if objects is None:
                objects = self._scenario_tree._scenario_map

        results = {}
        for object_name in objects:

            object_results = results[object_name] = {}
            auxilliary_values = object_results['auxilliary_values'] = {}
            auxilliary_values['time'] = self._solve_times[object_name]
            auxilliary_values['pyomo_solve_time'] = self._pyomo_solve_times[object_name]
            auxilliary_values['gaps'] = self._gaps[object_name]
            auxilliary_values['solution_status'] = self._solution_status[object_name]

            if object_name not in failures:

                if self._options.verbose:
                    print("Successfully solved %s=%s" % (object_type[:-1], object_name))

                if object_type == 'bundles':
                    solution = object_results['solution'] = {}
                    for scenario_name in self._scenario_tree.\
                           get_bundle(object_name).scenario_names:
                        scenario = self._scenario_tree.get_scenario(scenario_name)
                        solution[scenario._name] = \
                            scenario.package_current_solution(
                                 translate_ids=self._reverse_master_scenario_tree_id_map)

                else:
                    scenario = self._scenario_tree.get_scenario(object_name)
                    object_results['solution'] = \
                        scenario.package_current_solution(
                            translate_ids=self._reverse_master_scenario_tree_id_map)

        return results

    def _update_fixed_variables_for_client(self, object_name, fixed_variables):

        if self._options.verbose:
            if self._scenario_tree.contains_bundles():
                print("Received request to update fixed variables for "
                      "bundle="+object_name)
            else:
                print("Received request to update fixed variables for "
                      "scenario="+object_name)

        for node_name, node_fixed_vars in iteritems(fixed_variables):
            tree_node = self._scenario_tree.get_node(node_name)
            tree_node._fix_queue.update(node_fixed_vars)

        self.push_fix_queue_to_instances()

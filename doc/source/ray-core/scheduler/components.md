# Ray Scheduler Component Explanations

## Global Control Store (GCS)
- **Purpose**: Central metadata store and coordinator for the entire Ray cluster
- **Responsibilities**:
  - Maintains global cluster state
  - Stores node registration information
  - Tracks task statuses and schedules
  - Coordinates cluster-wide operations
  - Serves as the source of truth for the cluster
- **Implementation**: Located in `src/ray/gcs/`

## Raylet (Node Manager)
- **Purpose**: Node-level process running on each machine in the cluster
- **Responsibilities**:
  - Contains NodeManager
  - Communicates with local workers
  - Manages local resources
  - Coordinates task execution on the node
  - Interfaces with the GCS for cluster-wide coordination
- **Implementation**: Located in `src/ray/raylet/`

## ClusterTaskManager
- **Purpose**: Manages task scheduling across the cluster
- **Responsibilities**:
  - Queues tasks for execution
  - Dispatches tasks to available nodes
  - Interacts with ClusterResourceScheduler to find resources
  - Reports infeasible tasks to GCS for potential scaling
  - Handles task spillover to other nodes when local resources are insufficient
- **Implementation**: Located in `src/ray/raylet/scheduling/cluster_task_manager.h`

## LocalTaskManager
- **Purpose**: Manages task execution on a single node
- **Responsibilities**:
  - Handles task execution on a single node
  - Manages worker processes
  - Tracks local task dependencies
  - Allocates resources to local tasks
  - Dispatches tasks to workers
- **Implementation**: Located in `src/ray/raylet/local_task_manager.h`

## ClusterResourceScheduler
- **Purpose**: Allocates resources to tasks based on availability
- **Responsibilities**:
  - Makes scheduling decisions based on resource availability
  - Assigns tasks to nodes
  - Manages resource allocation constraints
  - Implements scheduling policies
  - Finds the best schedulable node for a task
- **Implementation**: Located in `src/ray/raylet/scheduling/cluster_resource_scheduler.h`

## ClusterResourceManager
- **Purpose**: Tracks resource availability across the cluster
- **Responsibilities**:
  - Provides view of all cluster resources
  - Updates node resource usage
  - Tracks available resources across nodes
  - Manages resource capacities
  - Handles node addition and removal
- **Implementation**: Located in `src/ray/raylet/scheduling/cluster_resource_manager.h`

## ResourceDemandScheduler
- **Purpose**: Handles cluster scaling decisions
- **Responsibilities**:
  - Part of the autoscaler
  - Determines when to scale the cluster
  - Launches or terminates nodes based on demand
  - Optimizes resource allocation
  - Ensures efficient scaling of the cluster
- **Implementation**: Located in `python/ray/autoscaler/_private/resource_demand_scheduler.py`

""" Operations on object flow graphs.

This module contains functions that operate on existing flow graphs. To create 
a new flow graph from Python code, see the `flow_graph_builder` module.

These graphs are sometimes called "concrete" or "raw" flow graphs to
distinguish them from other dataflow graphs in the Open Discovery system.
"""
from __future__ import absolute_import

import networkx as nx


def flatten(graph, copy=True):
    """ Recursively flatten an object flow graph.
    
    All embedded graphs are lifted to the root graph. This means that only 
    "atomic" (not traced) function calls will be preserved.
    """
    if copy:
        graph = graph.copy()
    object_notes = graph.graph['object_annotations']
    source, sink = graph.graph['source'], graph.graph['sink']
    
    for node in graph.nodes():
        subgraph = graph.node[node].get('graph', None)
        if not subgraph:
            continue
        subgraph = flatten(subgraph, copy=False)
        sub_object_notes = subgraph.graph['object_annotations']
        sub_source, sub_sink = subgraph.graph['source'], subgraph.graph['sink']
        object_notes.update(sub_object_notes)
        
        # First, add all nodes and edges from the subgraph.
        graph.add_nodes_from(subgraph.nodes_iter(data=True))
        graph.add_edges_from(subgraph.edges_iter(data=True))
        
        # Re-wire the incoming edges.
        for pred, _, data in graph.in_edges_iter(node, data=True):
            obj_id = data['id']
            for succ in sub_source[obj_id]:
                graph.add_edge(pred, succ, id=obj_id)
            del sub_source[obj_id]
        # Any remaining sources are attached to the super graph.
        source.update(sub_source)
        
        # Re-wire the outgoing edges.
        for _, succ, data in graph.out_edges_iter(node, data=True):
            obj_id = data['id']
            graph.add_edge(sub_sink[obj_id], succ, id=obj_id)
            del sub_sink[obj_id]
        # Any remaining sinks are attached to the super graph. 
        sink.update(sub_sink)
        
        # Finally, remove the original node (and its edges).
        graph.remove_node(node)
    
    return graph


def join(first, second, copy=True):
    """ Join two object flow graphs.
    
    Assumes that the graphs have been captured sequentially.
    """
    # Start with the first graph.
    graph = first.copy() if copy else first
    
    # Add all nodes and edges from the second graph.
    graph.add_nodes_from(second.nodes_iter(data=True))
    graph.add_edges_from(second.edges_iter(data=True))

    # Merge sources from second graph.
    for obj_id, nodes in second.graph['source'].items():
        sink = first.graph['sink'].get(obj_id)
        if sink:
            for node in nodes:
                graph.add_edge(sink, node, id=obj_id)
        else:
            sources = graph.graph['source'].setdefault(obj_id, [])
            sources.extend(nodes)
    
    # Merge object annotations and sinks from second graph.
    graph.graph['object_annotations'].update(second.graph['object_annotations'])
    graph.graph['sink'].update(second.graph['sink'])
    
    return graph
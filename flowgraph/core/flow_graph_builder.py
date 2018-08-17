# Copyright 2018 IBM Corp.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import

from collections import OrderedDict
from copy import deepcopy
import gc
import inspect, types

from ipykernel.jsonutil import json_clean
import networkx as nx
from traitlets import HasTraits, Bool, Dict, Instance, List, Unicode, default

from flowgraph.kernel.slots import get_slot
from flowgraph.trace.frame_util import get_class_module, get_class_qual_name
from flowgraph.trace.object_tracker import ObjectTracker
from flowgraph.trace.trace_event import TraceEvent, TraceCall, TraceReturn
from .annotator import Annotator
from .graphutil import node_name
from .flow_graph import new_flow_graph


class FlowGraphBuilder(HasTraits):
    """ Build an object flow graph from a stream of trace events.
    
    A flow graph is a directed acyclic multigraph that describes the flow of
    objects through a program. Its nodes are function calls and its edges
    are (pointers to) objects. The incoming edges of a node are arguments to
    the function and outgoing edges are arguments or return values.
    (If the function is pure, the outgoing edges are only return values.)
    """
    
    # Annotator for Python objects and functions.
    annotator = Instance(Annotator, args=())
    
    # Whether to store annotated slots for objects on creation or mutation.
    store_slots = Bool(True)
    
    # Private traits.
    _stack = List() # List(Instance(_CallContext))
    
    # Public interface
    
    def __init__(self, **traits):
        super(FlowGraphBuilder, self).__init__(**traits)
        self.reset()
    
    @property
    def graph(self):
        """ Top-level flow graph.
        """
        # Make a shallow copy.
        return nx.MultiDiGraph(self._stack[0].graph)
    
    def push_event(self, event):
        """ Push a new TraceEvent to the builder.
        """
        if isinstance(event, TraceCall):
            self._push_call_event(event)
        elif isinstance(event, TraceReturn):
            self._push_return_event(event)
        else:
            raise TypeError("Event must be TraceCall or TraceReturn")
    
    def reset(self):
        """ Reset the flow graph builder.
        """
        # The bottom of the call stack does not correspond to a call event.
        # It simply contains the root flow graph and associated state.
        graph = new_flow_graph()
        self._stack = [ _CallContext(graph=graph) ]
    
    def is_primitive(self, obj):
        """ Is the object considered primitive?
        
        Only primitive objects will be captured as "value" data for object slots
        and function inputs and outputs. (This does not preclude getting "id"
        data if object is also weak-referenceable.)
        
        Almost always, scalar types (bool, int, float, string, etc.) should be
        considered primitive. The default implementation allows any object which
        is JSON-able (essentially, the scalar types plus the built-in container
        types if their contents are JSON-able).
        
        Note: any objects stored as "value" data will be deep-copied.
        """
        try:
            json_clean(obj)
        except ValueError:
            return False
        return True
    
    def is_pure(self, event, annotation, arg_name):
        """ Is the call event pure with respect to the given argument?
        
        In a pure functional language (like Haskell) or a language with
        copy-on-modify semantics (like R), this would always be True, but in
        Python functions frequently mutate their arguments. Nevertheless,
        we regard the function as pure unless explicitly annotated otherwise.
        
        Of course, this convention is not really "correct", but the alternative
        is flow graphs with too many false positive mutations. In addition,
        even if we assumed mutating semantics by default, we don't have the
        complicated machinery to track downstream objects that may modify the
        original object, as in the following example:
        
            df = pandas.DataFrame(...)
            x = df.values
            x[:,0] = ...    # Also a mutation of `df`
        
        In view of these difficulties, we are basically punting on tracking
        object mutations. In the future we could consider adding
        object-specific heuristics to detect mutations, e.g., for data frames
        check the column names and dtypes or even, if the data is small enough,
        a hash of the underlying data.
        """
        # Special case: certain methods are never pure.
        func_name = event.qual_name.split('.')[-1]
        mutating_methods = ('__init__', '__setattr__', '__setitem__')
        if func_name in mutating_methods and arg_name == 'self':
            return False
        
        # Default: pure unless explicitly annotated otherwise!
        codomain = annotation.get('codomain', [])
        slots = _IOSlots(event)
        return not any(arg_name == slots._name(obj['slot']) for obj in codomain)
    
    # Protected interface
            
    def _push_call_event(self, event):
        """ Push a call event onto the stack.
        """
        # Get graph from context of previous call.
        context = self._stack[-1]
        graph = context.graph
        
        # Create a new node for this call.
        annotation = self.annotator.notate_function(event.function) or {}
        node = self._add_call_node(event, annotation)
        
        # Add edges for function arguments.
        object_tracker = event.tracer.object_tracker
        for arg_name, arg in event.arguments.items():
            self._add_call_in_edge(event, node, arg_name, arg)
            for value in self._hidden_referents(object_tracker, arg):
                self._add_call_in_edge(event, node, arg_name, value)
        
        # If the call is not atomic, we will enter a new scope.
        # Create a nested flow graph for the node.
        nested = None
        if not event.atomic:
            nested = new_flow_graph()
            graph.nodes[node]['graph'] = nested
    
        # Push call context onto stack.
        self._stack.append(_CallContext(event=event, node=node, graph=nested))
        
    def _push_return_event(self, event):
        """ Push a return event and pop the corresponding call from the stack.
        """
        # Pop matching call event from stack to retrieve node.
        context = self._stack.pop()
        if not context.event.full_name == event.full_name:
            # Sanity check
            raise RuntimeError("Mismatched trace events")
        node = context.node
                
        # Update node data for this call.
        annotation = self.annotator.notate_function(event.function) or {}
        if not self._update_call_node_for_return(event, annotation, node):
            return
        
        # Set output for return value(s).
        object_tracker = event.tracer.object_tracker
        return_value = event.return_value
        if isinstance(return_value, tuple):
            # Interpret tuples as multiple return values, per Python convention.
            for i, value in enumerate(return_value):
                value_id = object_tracker.get_id(value)
                if value_id:
                    self._set_object_output_node(
                        event, value, value_id, node, '__return__.%i' % i)
        else:
            # All other objects are treated as a single return value.
            return_id = object_tracker.get_id(return_value)
            if return_id:
                self._set_object_output_node(
                    event, return_value, return_id, node, '__return__')
        
        # Set outputs for mutated arguments.
        for arg_name, arg in event.arguments.items():
            arg_id = object_tracker.get_id(arg)
            if arg_id and not self.is_pure(event, annotation, arg_name):
                port = self._mutated_port_name(arg_name)
                self._set_object_output_node(event, arg, arg_id, node, port)
    
    def _add_call_node(self, event, annotation):
        """ Add a new call node for a call event.
        """
        context = self._stack[-1]
        graph = context.graph
        node = node_name(graph, event.qual_name)
        data = {
            'module': event.module,
            'qual_name': event.qual_name,
            'ports': self._get_ports_data(
                event,
                event.arguments.keys(),
                [ dom['slot'] for dom in annotation.get('domain', []) ],
                { 'portkind': 'input' },
            ),
        }
        if annotation:
            data.update({
                'annotation': self._annotation_key(annotation),
                'annotation_kind': 'function',
            })
        graph.add_node(node, **data)
        return node
    
    def _update_call_node_for_return(self, event, annotation, node):
        """ Update a call node for a return event.
        """
        context = self._stack[-1]
        graph = context.graph
        data = graph.nodes[node]
        
        # Handle special methods (unless overriden by annotation).
        if event.name in ('__getattr__', '__getattribute__'):
            # If attribute is actually a bound method, remove the call node.
            # Method objects are not tracked and the method will be traced when
            # it is called, so the getattr node is redundant and useless.
            if isinstance(event.return_value, types.MethodType):
                graph.remove_node(node)
                return False
            # Otherwise, record the attribute as a slot access.
            elif not annotation:
                self._update_getattr_node_for_return(event, node)
        
        elif event.name == '__init__' and not annotation:
            # Record the object initializer as a constructor.
            self._update_init_node_for_return(event, node)
        
        # Add output ports.
        port_names = []
        return_value = event.return_value
        if isinstance(return_value, tuple):
            for i in range(len(return_value)):
                port_names.append('__return__.%i' % i)
        elif return_value is not None:
            port_names.append('__return__')
        for arg_name in event.arguments.keys():
            if not self.is_pure(event, annotation, arg_name):
                port_names.append((arg_name, self._mutated_port_name(arg_name)))
        
        ports = data['ports']
        ports.update(self._get_ports_data(
            event,
            port_names,
            [ dom['slot'] for dom in annotation.get('codomain', []) ],
            { 'portkind': 'output' },
        ))
        
        return True

    def _update_getattr_node_for_return(self, event, node):
        """ Update a `__getattr__` call node for a return event.
        """
        context = self._stack[-1]
        data = context.graph.nodes[node]
        
        args = list(event.arguments.values())
        obj, name = args[0], args[1]
        note = self.annotator.notate_object(obj) or {}
        for slot_index, slot_def in enumerate(note.get('slots', [])):
            slot = slot_def['slot']
            if slot == name:
                data.update({
                    'slot': slot,
                    'annotation': self._annotation_key(note),
                    'annotation_index': slot_index+1,
                    'annotation_kind': 'slot',
                })
                break
        else:
            data['slot'] = name
    
    def _update_init_node_for_return(self, event, node):
        """ Update an `__init__` call node for a return event.
        """
        context = self._stack[-1]
        data = context.graph.nodes[node]
        
        obj = list(event.arguments.values())[0]
        note = self.annotator.notate_object(obj)
        if note:
            data.update({
                'annotation': self._annotation_key(note),
                'annotation_kind': 'construct',
            })
        else:
            data['construct'] = True
    
    def _add_call_in_edge(self, event, node, arg_name, arg):
        """ Add an incoming edge to a call node.
        """
        # Only proceed if the argument is tracked.
        arg_id = event.tracer.object_tracker.get_id(arg)
        if not arg_id:
            return
        
        # Add edge if the argument has a known output node.
        context = self._stack[-1]
        graph = context.graph
        src, src_port = self._get_object_output_node(arg_id)
        if src is not None:
            self._add_object_edge(arg, arg_id, src, node,
                                  sourceport=src_port, targetport=arg_name)
        
        # Otherwise, mark the argument as an unknown input.
        #
        # Exceptions: Do not treat the following as inputs.
        #   1) `self` argument in constructors
        #   2) `cls` argument in class methods
        elif not (event.atomic and 
                  ((arg_name == 'self' and event.name == '__init__') or
                   (arg_name == 'cls' and inspect.ismethod(event.function) and
                    event.function.__self__ is arg))):
            self._add_object_input_node(arg, arg_id, node, arg_name)
    
    def _add_object_edge(self, obj, obj_id, source, target, 
                         sourceport=None, targetport=None):
        """ Add an edge corresponding to an object.
        """
        context = self._stack[-1]
        graph = context.graph
        note = self.annotator.notate_object(obj)
        data = { 'id': obj_id }
        if note:
            data['annotation'] = self._annotation_key(note)
        if sourceport is not None:
            data['sourceport'] = sourceport
        if targetport is not None:
            data['targetport'] = targetport
        graph.add_edge(source, target, **data)
    
    def _add_object_input_node(self, obj, obj_id, node, port):
        """ Add an object as an unknown input to a node.
        """
        context = self._stack[-1]
        graph = context.graph
        input_node = graph.graph['input_node']
        self._add_object_edge(obj, obj_id, input_node, node, targetport=port)
    
    def _get_object_output_node(self, obj_id):
        """ Get the node/port of which the object is an output, if any. 
        
        An object is an "output" of a call node if it is the last node to have
        created/mutated the object.
        """
        context = self._stack[-1]
        output_table = context.output_table
        return output_table.get(obj_id, (None, None))
    
    def _set_object_output_node(self, event, obj, obj_id, node, port):
        """ Set an object as an output of a node.
        """
        context = self._stack[-1]
        graph, output_table = context.graph, context.output_table
        output_node = graph.graph['output_node']
        
        # Remove old output, if any.
        if obj_id in output_table:
            old, _ = output_table[obj_id]
            edge_data = graph.get_edge_data(old, output_node)
            keys = [ key for key, data in edge_data.items()
                     if data['id'] == obj_id ]
            assert len(keys) == 1
            graph.remove_edge(old, output_node, key=keys[0])
        
        # Set new output.
        output_table[obj_id] = (node, port)
        self._add_object_edge(obj, obj_id, node, output_node, sourceport=port)
        
        # The object has been created or mutated, so fetch its slots.
        if self.store_slots:
            self._add_object_slots(event, obj, obj_id, node, port)
    
    def _add_object_slots(self, event, obj, obj_id, node, port):
        """ Add nodes and edges for annotated slots of an object.
        """
        context = self._stack[-1]
        graph = context.graph
        note = self.annotator.notate_object(obj) or {}
        for slot_index, slot_def in enumerate(note.get('slots', [])):
            slot = slot_def['slot']
            try:
                slot_value = get_slot(obj, slot)
            except AttributeError:
                continue
            slot_node = node_name(graph, 'slot')
            slot_node_data = {
                'slot': slot,
                'annotation': self._annotation_key(note),
                'annotation_index': slot_index+1,
                'annotation_kind': 'slot',
                'ports': OrderedDict([
                    ('self', self._get_port_data(event, obj,
                        portkind='input',
                        annotation_index=1,
                    )),
                    ('__return__', self._get_port_data(event, slot_value,
                        portkind='output',
                        annotation_index=1,
                    )),
                ])
            }
            graph.add_node(slot_node, **slot_node_data)
            self._add_object_edge(obj, obj_id, node, slot_node,
                                  sourceport=port, targetport='self')
            
            # If object is trackable, recursively set it as output.
            if ObjectTracker.is_trackable(slot_value):
                slot_id = event.tracer.track_object(slot_value)
                self._set_object_output_node(
                    event, slot_value, slot_id, slot_node, '__return__')
    
    def _get_ports_data(self, event, names, annotation=[], extra_data={}):
        """ Get data for the ports (input or output) of a node.
        """
        ports = OrderedDict()
        slots = _IOSlots(event)
        annotation_table = { 
            # Index annotation domain starting at 1: it is language-agnostic.
            slots._name(slot): i+1 for i, slot in enumerate(annotation)
        }
        for name in names:
            name, portname = name if isinstance(name, tuple) else (name, name)
            try:
                obj = get_slot(slots, name)
            except AttributeError:
                obj = None
            
            data = self._get_port_data(event, obj, argname=name, **extra_data)
            if name in annotation_table:
                data['annotation_index'] = annotation_table[name]
            ports[portname] = data
        return ports
    
    def _get_port_data(self, event, obj, **extra_data):
        """ Get data for a single port on a node.
        """
        data = extra_data
        if obj is None:
            return data
        
        # Add object ID if available.
        obj_id = event.tracer.object_tracker.get_id(obj)
        if obj_id is not None:
            data['id'] = obj_id
        
        # Add value if the object is primitive.
        if self.is_primitive(obj):
            data['value'] = deepcopy(obj)
        
        # Add type information if type is not built-in.
        obj_type = obj.__class__
        module = get_class_module(obj_type)
        if not module == 'builtins':
            data['module'] = module
            data['qual_name'] = get_class_qual_name(obj_type)

        # Add object annotation, if it exists.
        note = self.annotator.notate_object(obj)
        if note:
            data['annotation'] = self._annotation_key(note)
        
        return data
    
    def _annotation_key(self, note):
        """ Get a key identifying an annotation.
        """
        keys = ('language', 'package', 'id')
        return '/'.join(note[key] for key in keys)
    
    def _hidden_referents(self, tracker, obj):
        """ Get "hidden" referents of an object.
        
        The Python container types `tuple`, `list`, and `dict` are not
        weak-referenceable and hence not trackable by `ObjectTracker`. In fact,
        not even subclasses of `tuple` are weak-referenceable! Moreover,
        `Tracer` does not produce trace events for `tuple`, `list`, `dict`, and
        `set` literals or comprehensions (because `sys.settrace` does not).
        Consequently the objects belonging to (referenced by) these containers
        are "hidden" from our system.
        
        This method uses the Python garbage collector to find tracked objects
        referred to by untrackable containers.
        
        See also `Tracker.is_trackable()`.
        """
        # FIXME: This whole method is a hack. We should find a better way to
        # solve this problem.
        if (isinstance(obj, (tuple, list, dict, set, frozenset)) and
            not ObjectTracker.is_trackable(obj)):
            for referent in gc.get_referents(obj):
                if tracker.is_tracked(referent):
                    yield referent
    
    def _mutated_port_name(self, arg_name):
        """ Get name of output port for a mutated argument.
        
        Because the GraphML protocol does not support input and output ports as
        first-class entities, the names of ports must be unique across inputs
        and outputs. Therefore, when an argument is mutated and hence appears as
        both an input and output, we must give the output port a different name.
        """
        return arg_name + '!'


class _CallContext(HasTraits):
    """ Context for a trace call event.
    
    Internal state for FlowGraphBuilder.
    """
    # The trace call event for this call stack item.
    event = Instance(TraceCall)
    
    # Name of graph node created for call, if any.
    node = Unicode()
    
    # Flow graph nested in node, if any.
    graph = Instance(nx.MultiDiGraph, allow_none=True)
    
    # Output table for the flow graph.
    #
    # At any given time during execution, an object is the output of at most one
    # node, i.e., there is at most one incoming edge to the special output node
    # that carries a particular object. We maintain this mapping as an auxiliary
    # data structure called the "output table". It is logically superfluous--the
    # same information is captured by the graph topology--but it improves
    # efficiency by allowing constant-time lookup.
    output_table = Dict()


class _IOSlots(object):
    """ Get slots of a function call or return event.
    
    Implementation detail of FlowGraphBuilder.
    """

    def __init__(self, event):
        self.__event = event
    
    def _name(self, slot):
        """ Map the function slot (integer or string) to a string name, if any.
        """
        event = self.__event
        if isinstance(slot, int):
            argument_names = list(event.arguments.keys())
            try:
                return argument_names[slot]
            except IndexError:
                return None
        return slot
    
    def __getattr__(self, name):
        event = self.__event
        if name == '__return__':
            return event.return_value
        try:
            return event.arguments[name]
        except KeyError:
            raise AttributeError("No function slot %r" % name)
    
    def __getitem__(self, index):
        event = self.__event
        argument_names = list(event.arguments.keys())
        return event.arguments[argument_names[index]]
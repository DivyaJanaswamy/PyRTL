
"""
Passes contains structures helpful for writing analysis and
transformation passes over blocks.
"""

from __future__ import print_function, unicode_literals

from .core import working_block, set_working_block, debug_mode, LogicNet, PostSynthBlock
from .helperfuncs import find_and_print_loop, as_wires, concat_list
from .memory import MemBlock
from .pyrtlexceptions import PyrtlError, PyrtlInternalError
from .wire import WireVector, Input, Output, Const, Register
from .transform import net_transform, _get_new_block_mem_instance


# --------------------------------------------------------------------
#   __   __  ___           __      ___    __
#  /  \ |__)  |  |  |\/| |  /  /\   |  | /  \ |\ |
#  \__/ |     |  |  |  | | /_ /~~\  |  | \__/ | \|
#


def optimize(update_working_block=True, block=None):
    """ Return an optimized version of a synthesized hardware block. """
    block = working_block(block)
    if not update_working_block:
        from .transform import copy_block
        block = copy_block(block)

    if debug_mode:
        block.sanity_check()
        _remove_wire_nets(block)
        block.sanity_check()
        _constant_propagation(block)
        block.sanity_check()
        _remove_unlistened_nets(block)
    else:
        _remove_wire_nets(block)
        _constant_propagation(block)
        _remove_unlistened_nets(block)
    return block


def _remove_wire_nets(block):
    """ Remove all wire nodes from the block. """

    def find_producer(x):
        # trace back to the root producer of x
        if x in immediate_producer:
            return find_producer(immediate_producer[x])
        else:
            return x

    immediate_producer = {}  # map from wirevector to its direct producer wirevector
    wire_removal_set = set()  # set of all wirevectors to be removed

    # one pass to build the map of value producers and
    # all of the nets and wires to be removed
    for net in block.logic:
        if net.op == 'w':
            immediate_producer[net.dests[0]] = net.args[0]
            if not isinstance(net.dests[0], Output):
                wire_removal_set.add(net.dests[0])

    # second full pass to create the new logic without the wire nets
    new_logic = set()
    for net in block.logic:
        if net.op != 'w' or isinstance(net.dests[0], Output):
            new_args = tuple(find_producer(x) for x in net.args)
            new_net = LogicNet(net.op, net.op_param, new_args, net.dests)
            new_logic.add(new_net)

    # now update the block with the new logic and remove wirevectors
    block.logic = new_logic
    for dead_wirevector in wire_removal_set:
        del block.wirevector_by_name[dead_wirevector.name]
        block.wirevector_set.remove(dead_wirevector)

    block.sanity_check()


def _constant_propagation(block):
    """ Removes excess constants in the block.

    Note on resulting block:
    The output of the block can have wirevectors that are driven but not
    listened to. This is to be expected. These are to be removed by the
    _remove_unlistened_nets function
    """

    current_nets = 0
    while len(block.logic) != current_nets:
        current_nets = len(block.logic)
        _constant_prop_pass(block)


def _constant_prop_pass(block):
    """ Does one constant propagation pass """

    def constant_prop_check(net_checking):

        def replace_net(new_net):
            nets_to_remove.add(net_checking)
            nets_to_add.add(new_net)

        def replace_net_with_const(const_val):
            new_const_wire = Const(bitwidth=1, val=const_val, block=block)
            wire_add_set.add(new_const_wire)
            replace_net_with_wire(new_const_wire)

        def replace_net_with_wire(new_wire):
            if isinstance(net_checking.dests[0], Output):
                replace_net(LogicNet('w', None, args=(new_wire,),
                                     dests=net_checking.dests))
            else:
                nets_to_remove.add(net_checking)
                replacement_wires[net_checking.dests[0]] = new_wire

        one_var_ops = {
            '~': lambda x: 1-x,
            'r': lambda x: x   # This is only valid for constant folding purposes
        }
        two_var_ops = {
            '&': lambda l, r: l & r,
            '|': lambda l, r: l | r,
            '^': lambda l, r: l ^ r,
            'n': lambda l, r: 1-(l & r),
        }
        num_constants = sum((isinstance(arg_wire, Const)
                            for arg_wire in net_checking.args))

        if num_constants is 0 or net_checking.op == 'w':
            return

        if (net_checking.op in two_var_ops) & num_constants is 1:
            # special case
            arg1, arg2 = net_checking.args
            if isinstance(arg1, Const):
                const_wire = arg1
                other_wire = arg2
            else:
                const_wire = arg2
                other_wire = arg1

            outputs = [two_var_ops[net_checking.op](const_wire.val, other_val)
                       for other_val in range(2)]

            if outputs[0] == outputs[1]:
                replace_net_with_const(outputs[0])
            elif outputs[0] == 0:
                replace_net_with_wire(other_wire)
            else:
                replace_net(LogicNet('~', None, args=(other_wire,),
                                     dests=net_checking.dests))

        else:
            if net_checking.op in two_var_ops:
                output = two_var_ops[net_checking.op](net_checking.args[0].val,
                                                      net_checking.args[1].val)
            elif net_checking.op in one_var_ops:
                output = one_var_ops[net_checking.op](net_checking.args[0].val)
            else:
                # this is for nets that we are not modifying (eg spliting, and memory)
                return
            replace_net_with_const(output)

    def find_producer(x):
        # trace back to the root producer of x
        if x in replacement_wires:
            return find_producer(replacement_wires[x])
        else:
            return x

    replacement_wires = {}  # map from wire to its producer
    wire_add_set = set()
    nets_to_add = set()
    nets_to_remove = set()

    for a_net in block.logic:
        constant_prop_check(a_net)
    # second full pass to cleanup

    new_logic = set()
    for net in block.logic:
        if net not in nets_to_remove:
            new_args = tuple(find_producer(x) for x in net.args)
            new_net = LogicNet(net.op, net.op_param, new_args, net.dests)
            new_logic.add(new_net)
    # now update the block with the new logic and remove wirevectors

    new_logic = new_logic.union(nets_to_add)
    block.logic = new_logic
    for new_wirevector in wire_add_set:
        block.add_wirevector(new_wirevector)

    _remove_unused_wires(block, "constant folding")


def _remove_unlistened_nets(block):
    """ Removes all nets that are not connected to an output wirevector
    """

    listened_nets = set()
    listened_wires_cur = set()
    prev_listened_net_count = 0

    for a_net in block.logic:
        if a_net.op in 'm@':
            listened_nets.add(a_net)
            for arg_wire in a_net.args:
                listened_wires_cur.add(arg_wire)
        elif isinstance(a_net.dests[0], Output):
            listened_nets.add(a_net)
            for arg_wire in a_net.args:
                listened_wires_cur.add(arg_wire)

    while len(listened_nets) > prev_listened_net_count:
        prev_listened_net_count = len(listened_nets)
        listened_wires_prev = listened_wires_cur

        for net in block.logic:
            if net not in listened_nets:
                if any((destWire in listened_wires_prev) for destWire in net.dests):
                    listened_nets.add(net)
                    for arg_wire in net.args:
                        listened_wires_cur.add(arg_wire)

    # now I need to add back the interface for the inputs that were removed
    for net in block.logic:
        if net.op == 's' and isinstance(net.args[0], Input) and net not in listened_nets:
            listened_nets.add(net)
            # notify the user that this net is useless

    block.logic = listened_nets
    _remove_unused_wires(block, "unlistened net removal")


def _remove_unused_wires(block, parent_process_name):
    """ Removes all unconnected wires from a block"""
    valid_wires = set()
    for logic_net in block.logic:
        valid_wires.update(logic_net.args, logic_net.dests)

    wire_removal_set = block.wirevector_set.difference(valid_wires)
    for removed_wire in wire_removal_set:
        if isinstance(removed_wire, Input):
            print("Input Wire, " + removed_wire.name + " was removed by " + parent_process_name)
        if isinstance(removed_wire, Output):
            PyrtlInternalError("Output wire, " + removed_wire.name +
                               "was disconnected by" + parent_process_name)

    block.wirevector_set = valid_wires

# --------------------------------------------------------------------
#    __           ___       ___  __     __
#   /__` \ / |\ |  |  |__| |__  /__` | /__`
#   .__/  |  | \|  |  |  | |___ .__/ | .__/
#


def synthesize(update_working_block=True, block=None):
    """ Lower the design to just single-bit "and", "or", and "not" gates.

    :param update_working_block: Boolean specifying if working block update
    :param block: The block you want to synthesize
    :return: The newly synthesized block (of type PostSynthesisBlock).

    Takes as input a block (default to working block) and creates a new
    block which is identical in function but uses only single bit gates
    and excludes many of the more complicated primitives.  The new block
    should consist *almost* exclusively of the combination elements
    of w, &, |, ^, and ~ and sequential elements of registers (which are
    one bit as well).  The two exceptions are for inputs/outputs (so that
    we can keep the same interface) which are immediately broken down into
    the individual bits and memories.  Memories (read and write ports) which
    require the reassembly and disassembly of the wirevectors immediately
    before and after.  There arethe only two places where 'c' and 's' ops
    should exist.

    The block that results from synthesis is actually of type
    "PostSynthesisBlock" which contains a mapping from the original inputs
    and outputs to the inputs and outputs of this block.  This is used during
    simulation to map the input/outputs so that the same testbench can be
    used both pre and post synthesis (see documentation for Simulation for
    more details).
    """

    block_in = working_block(block)
    block_in.sanity_check()  # before going further, make sure that pressynth is valid
    block_out = PostSynthBlock()
    # resulting block should only have one of a restricted set of net ops
    block_out.legal_ops = set('~&|^nrwcsm@')
    wirevector_map = {}  # map from (vector,index) -> new_wire
    io_map = block_out.io_map  # map from presynth inputs and outputs to postsynth i/o
    uid = 0  # used for unique names

    # First step, create all of the new wires for the new block
    # from the original wires and store them in the wirevector_map
    # for reference.
    for wirevector in block_in.wirevector_subset():
        for i in range(len(wirevector)):
            new_name = '_'.join((wirevector.name, 'synth', str(i), str(uid)))
            uid += 1
            if isinstance(wirevector, Const):
                new_val = (wirevector.val >> i) & 0x1
                new_wirevector = Const(bitwidth=1, val=new_val, block=block_out)
            elif isinstance(wirevector, (Input, Output)):
                new_wirevector = WireVector(name="tmp_" + new_name, bitwidth=1, block=block_out)
            else:
                new_wirevector = wirevector.__class__(name=new_name, bitwidth=1, block=block_out)
            wirevector_map[(wirevector, i)] = new_wirevector

    # Now connect up the inputs and outputs to maintain the interface
    for wirevector in block_in.wirevector_subset(Input):
        input_vector = Input(name=wirevector.name, bitwidth=len(wirevector), block=block_out)
        io_map[wirevector] = input_vector
        for i in range(len(wirevector)):
            wirevector_map[(wirevector, i)] <<= input_vector[i]
    for wirevector in block_in.wirevector_subset(Output):
        output_vector = Output(name=wirevector.name, bitwidth=len(wirevector), block=block_out)
        io_map[wirevector] = output_vector
        # the "reversed" is needed because most significant bit comes first in concat
        output_bits = [wirevector_map[(wirevector, i)]
                       for i in range(len(output_vector))]
        output_vector <<= concat_list(output_bits)

    # Now that we have all the wires built and mapped, walk all the blocks
    # and map the logic to the equivalent set of primitives in the system
    out_mems = block_out.mem_map  # dictionary: PreSynth Map -> PostSynth Map
    for net in block_in.logic:
        _decompose(net, wirevector_map, out_mems, block_out)

    if update_working_block:
        set_working_block(block_out)
    return block_out


def _decompose(net, wv_map, mems, block_out):
    """ Add the wires and logicnets to block_out and wv_map to decompose net """

    def arg(x, i):
        # return the mapped wire vector for argument x, wire number i
        return wv_map[(net.args[x], i)]

    def destlen():
        # return iterator over length of the destination in bits
        return range(len(net.dests[0]))

    def assign_dest(i, v):
        # assign v to the wiremap for dest[0], wire i
        wv_map[(net.dests[0], i)] <<= v

    one_var_ops = {
        'w': lambda w: w,
        '~': lambda w: ~w,
    }
    c_two_var_ops = {
        '&': lambda l, r: l & r,
        '|': lambda l, r: l | r,
        '^': lambda l, r: l ^ r,
        'n': lambda l, r: l.nand(r),
    }

    if net.op in one_var_ops:
        for i in destlen():
            assign_dest(i, one_var_ops[net.op](arg(0, i)))
    elif net.op in c_two_var_ops:
        for i in destlen():
            assign_dest(i, c_two_var_ops[net.op](arg(0, i), arg(1, i)))
    elif net.op == '=':
        # The == operator is implemented with a nor of xors.
        temp_result = arg(0, 0) ^ arg(1, 0)
        for i in range(1, len(net.args[0])):
            temp_result = temp_result | (arg(0, i) ^ arg(1, i))
        assign_dest(0, ~temp_result)
    elif net.op == 'x':
        for i in destlen():
            muxed_bit = ~arg(0, 0) & arg(1, i) | arg(0, 0) & arg(2, i)
            assign_dest(i, muxed_bit)
    elif net.op == 's':
        for i in destlen():
            selected_bit = arg(0, net.op_param[i])
            assign_dest(i, selected_bit)
    elif net.op == 'c':
        arg_wirelist = []
        # generate list of wires for vectors being concatenated
        for arg_vector in net.args:
            arg_vector_as_list = [wv_map[(arg_vector, i)] for i in range(len(arg_vector))]
            arg_wirelist = arg_vector_as_list + arg_wirelist
        for i in destlen():
            assign_dest(i, arg_wirelist[i])
    elif net.op == 'r':
        for i in destlen():
            args = (arg(0, i),)
            dests = (wv_map[(net.dests[0], i)],)
            new_net = LogicNet('r', None, args=args, dests=dests)
            block_out.add_net(new_net)
    elif net.op == '+':
        arg0list = [arg(0, i) for i in range(len(net.args[0]))]
        arg1list = [arg(1, i) for i in range(len(net.args[1]))]
        cin = Const(0, bitwidth=1, block=block_out)
        sumbits, cout = _generate_add(arg0list, arg1list, cin)
        destlist = sumbits + [cout]
        for i in destlen():
            assign_dest(i, destlist[i])
    elif net.op == '>':
        # where xi = Ai==Bi then
        # A>B = A3 & ~B3 | A2 & ~B2 & x3 | A1 & ~B1 & x3 & x2 | A0 & ~B0 & x3 & x2 & x1
        bitlen = len(net.args[0])
        # Compute the xi above, but don't compute x0 (put None in it's place)
        x = [~(arg(0, i) ^ arg(1, i)) for i in range(1, bitlen)]
        x.insert(0, None)
        # OR over all the terms
        result = None
        for i in range(0, bitlen):
            term = arg(0, i) & ~arg(1, i)
            for j in range(i+1, bitlen):
                term = term & x[j]
            result = (term) if result is None else (result | term)
        assign_dest(0, result)
    elif net.op == '<':
        # where xi = Ai==Bi then
        # A<B = ~A3 & B3 | ~A2 & B2 & x3 | ~A1 & B1 & x3 & x2 | ~A0 & B0 & x3 & x2 & x1
        bitlen = len(net.args[0])
        # Compute the xi above, but don't compute x0 (put None in it's place)
        x = [~(arg(0, i) ^ arg(1, i)) for i in range(1, bitlen)]
        x.insert(0, None)
        # OR over all the terms
        result = None
        for i in range(0, bitlen):
            term = ~arg(0, i) & arg(1, i)
            for j in range(i+1, bitlen):
                term = term & x[j]
            result = (term) if result is None else (result | term)
        assign_dest(0, result)
    elif net.op == '-':
        arg0list = [arg(0, i) for i in range(len(net.args[0]))]
        arg1list = [~arg(1, i) for i in range(len(net.args[1]))]
        cin = Const(1, bitwidth=1, block=block_out)
        sumbits, cout = _generate_add(arg0list, arg1list, cin)
        destlist = sumbits + [cout]
        for i in destlen():
            assign_dest(i, destlist[i])
    elif net.op == 'm':
        arg0list = [arg(0, i) for i in range(len(net.args[0]))]
        addr = concat_list(arg0list)
        new_mem = _get_new_block_mem_instance(net.op_param, mems, block_out)[1]
        data = as_wires(new_mem[addr])
        for i in destlen():
            assign_dest(i, data[i])
    elif net.op == '@':
        addrlist = [arg(0, i) for i in range(len(net.args[0]))]
        addr = concat_list(addrlist)
        datalist = [arg(1, i) for i in range(len(net.args[1]))]
        data = concat_list(datalist)
        enable = arg(2, 0)
        new_mem = _get_new_block_mem_instance(net.op_param, mems, block_out)[1]
        new_mem[addr] <<= MemBlock.EnabledWrite(data=data, enable=enable)
    else:
        raise PyrtlInternalError('Unable to synthesize the following net '
                                 'due to unimplemented op :\n%s' % str(net))
    return


def nand_synth(block=None):
    """
    Synthesizes an Post-Synthesis block into one consisting of nands and inverters in place
    :param block: The block to synthesize.
    """
    def nand_synth_op(net):
        if net.op in '~nrwcsm@':
            return True

        def arg(num):
            return net.args[num]

        dest = net.dests[0]
        if net.op == '&':
            dest <<= ~(arg(0).nand(arg(1)))
        elif net.op == '|':
            dest <<= (~arg(0)).nand(~arg(1))
        elif net.op == '^':
            temp_0 = arg(0).nand(arg(1))
            dest <<= temp_0.nand(arg(0)).nand(temp_0.nand(arg(1)))
        else:
            raise PyrtlError("Op, '{}' is not supported in nand_synth".format(net.op))

    net_transform(nand_synth_op, block)


def and_inverter_synth(block=None):
    """
    Transforms a decomposed block into one consisting of ands and inverters in place
    :param block: The block to synthesize
    """
    def and_inv_op(net):
        if net.op in '~&rwcsm@':
            return True

        def arg(num):
            return net.args[num]

        dest = net.dests[0]
        if net.op == '|':
            dest <<= ~(~arg(0) & ~arg(1))
        elif net.op == '^':
            all_1 = arg(0) & arg(1)
            all_0 = ~arg(0) & ~arg(1)
            dest <<= all_0 & ~all_1
        elif net.op == 'n':
            dest <<= ~(arg(0) & arg(1))
        else:
            raise PyrtlError("Op, '{}' is not supported in and_inv_synth".format(net.op))

    net_transform(and_inv_op, block)


def _generate_one_bit_add(a, b, cin):
    """ Generates hardware for a 1-bit full adder.
    :param a, b, cin: 3 1-bit wire vectors
    :return a list of wire vectors (the sum), and a single 1-bit wirevector cout
    """
    sumbit = a ^ b ^ cin
    cout = a & b | a & cin | b & cin
    return [sumbit], cout


def _generate_add(a, b, cin):
    """ a and b are lists of wirevectors (all len 1)
        cin is a wirevector (also len 1)
        returns sum as list of wirevectors (all len 1)
        and a carry out wirevector (also len 1)
    """
    if len(a) == 1:
        sumbits, cout = _generate_one_bit_add(a[0], b[0], cin)
    else:
        lsbit, ripplecarry = _generate_one_bit_add(a[0], b[0], cin)
        msbits, cout = _generate_add(a[1:], b[1:], ripplecarry)
        sumbits = lsbit + msbits  # append to lsb to the lowest bits
    return sumbits, cout

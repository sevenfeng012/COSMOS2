from inspect import getargspec
import os
import re
import itertools as it
import operator
from collections import OrderedDict
import types

from .. import Task, NOOP
# from ..models.TaskFile import InputFileAssociation, AbstractInputFile, AbstractOutputFile
from ..util.helpers import str_format, has_duplicates, strip_lines
from .files import find


class ToolValidationError(Exception): pass


opj = os.path.join

OPS = OrderedDict([("<=", operator.le),
                   ("<", operator.lt),
                   (">=", operator.ge),
                   (">", operator.gt),
                   ('==', operator.eq),
                   ("=", operator.eq)])


def parse_aif_cardinality(n):
    op, number = re.search('(.*?)(\d+)', str(n)).groups()
    if op == '':
        op = '=='
    number = int(number)
    return op, number


class _ToolMeta(type):
    def __init__(cls, name, bases, dct):
        cls.name = name
        return super(_ToolMeta, cls).__init__(name, bases, dct)


def error(message, dct):
    import sys

    print >> sys.stderr, '******ERROR*******'
    print >> sys.stderr, '    %s' % message
    for k, v in dct.items():
        print >> sys.stderr, '***%s***' % k
        if isinstance(v, list):
            for v2 in v:
                print >> sys.stderr, "    %s" % v2
        elif isinstance(v, dict):
            for k2, v2 in v.items():
                print >> sys.stderr, "    %s = %s" % (k2, v2)
        else:
            print >> sys.stderr, "    %s" % v

    raise ToolValidationError(message)


class Tool(object):
    """
    Essentially a factory that produces Tasks.  :meth:`cmd` must be overridden unless it is a NOOP task.
    """
    __metaclass__ = _ToolMeta

    mem_req = None
    time_req = None
    cpu_req = None
    must_succeed = True
    # NOOP = False
    persist = False
    drm = None
    skip_profile = False
    abstract_inputs = []  # class property!  Does not change per instance.
    abstract_outputs = []  # class property!  Does not change per instance.
    output_dir = None

    def __init__(self, tags, parents=None, out=''):
        """
        :param tags: (dict) A dictionary of tags.  The combination of tags are the unique identifier for a Task,
            and must be unique for any tasks in its stage.  They are also passed as parameters to the cmd() call.  Tag
            values must be basic python types.
        :param parents: (list of Tasks).  A list of parent tasks
        :param out: an output directory, will be .format()ed with tags
        """
        assert isinstance(tags, dict), '`tags` must be a dict'
        assert isinstance(out, basestring), '`out` must be a str'
        if isinstance(parents, types.GeneratorType):
            parents = list(parents)
        if parents is None:
            parents = []

        if issubclass(parents.__class__, Task):
            parents = [parents]
        else:
            parents = list(parents)

        assert hasattr(parents, '__iter__'), 'Tried to set %s.parents to %s which is not iterable' % (self, parents)
        assert all(issubclass(p.__class__, Task) for p in parents), 'parents must be an iterable of Tasks or a Task'

        parents = filter(lambda p: p is not None, parents)

        self.tags = tags.copy()  # can't expect the User to remember to do this.
        self.__validate()
        self.load_sources = []  # for Inputs

        self.out = out
        self.task_parents = parents if parents else []

        argspec = getargspec(self.cmd)
        self.input_arg_map = OrderedDict()
        self.output_arg_map = OrderedDict()

        # iterate over argspec keywords and their defaults
        for kw, default in zip(argspec.args[-len(argspec.defaults or []):], argspec.defaults or []):
            if isinstance(kw, list):
                # for when user specifies unpacking in a parameter name
                kw = frozenset(kw)

            if kw.startswith('in_') or isinstance(default, find):
                self.input_arg_map[kw] = default
            elif kw.startswith('out_') or isinstance(default, find):
                self.output_arg_map[kw] = default

        self.abstract_inputs = self.input_arg_map.values()
        self.abstract_outputs = self.output_arg_map.values()

    def __validate(self):
        # assert all(i.__class__.__name__ == 'AbstractInputFile' for i in
        #            self.abstract_inputs), '%s Tool.abstract_inputs must be of type AbstractInputFile' % self
        # assert all(o.__class__.__name__ == 'AbstractOutputFile' for o in
        #            self.abstract_outputs), '%s Tool.abstract_outputs must be of type AbstractOutputFile' % self

        # if has_duplicates([(i.name, i.format) for i in self.abstract_inputs]):
        #     raise ToolValidationError("Duplicate task.abstract_inputs detected in {0}".format(self))
        #
        # if has_duplicates([(i.name, i.format) for i in self.abstract_outputs]):
        #     raise ToolValidationError("Duplicate task.abstract_outputs detected in {0}".format(self))

        # reserved = {'name', 'format', 'basename'}
        # if not set(self.tags.keys()).isdisjoint(reserved):
        #     raise ToolValidationError(
        #         "%s are a reserved names, and cannot be used as a tag keyword in %s" % (reserved, self))

        from cosmos import ERROR_IF_TAG_IS_NOT_BASIC_TYPE
        if ERROR_IF_TAG_IS_NOT_BASIC_TYPE:
            for k,v in self.tags.iteritems():
                # msg = '%s.tags[%s] is not a basic python type.  ' \
                #       'Tag values should be a str, int, float or bool.' \
                #       'Alternatively, you can set cosmos.ERROR_OF_TAG_IS_NOT_BASIC_TYPE = False. \'' \
                #       'IF YOU ENABLE THIS, TAGS THAT ARE NOT BASIC TYPES WILL ONLY BE USED AS PARAMETERS TO THE cmd()' \
                #       'FUNCTION, AND NOT FOR MATCHING PREVIOUSLY SUCCESSFUL TASKS WHEN RESUMING OR STORED IN THE' \
                #       'SQL DB.' % (self,k)
                msg = '%s.tags[%s] is not a basic python type.  ' \
                      'Tag values should be a str, int, float or bool.' % (self,k)
                assert any(isinstance(v, t) for t in [basestring, int, float, bool]), msg

    def _validate_input_mapping(self, abstract_input_file, mapped_input_taskfiles, parents):
        real_count = len(mapped_input_taskfiles)
        op, number = parse_aif_cardinality(abstract_input_file.n)

        if not OPS[op](real_count, int(number)):
            s = '******ERROR****** \n' \
                '{self} does not have right number of inputs: for {abstract_input_file}\n' \
                '***Parents*** \n' \
                '{prnts}\n' \
                '***Inputs Matched ({real_count})*** \n' \
                '{mit} '.format(mit="\n".join(map(str, mapped_input_taskfiles)),
                                prnts="\n".join(map(str, parents)), **locals())
            import sys

            print >> sys.stderr, s
            raise ToolValidationError('Input files are missing, or their cardinality do not match.')

    # def _map_inputs(self, parents):
    #     """
    #     Default method to map abstract_inputs.  Can be overriden if a different behavior is desired
    #     :returns: [(taskfile, is_forward), ...]
    #     """
    #     for aif_index, abstract_input_file in enumerate(self.abstract_inputs):
    #         mapped_input_taskfiles = list(set(self._map_input(abstract_input_file, parents)))
    #         self._validate_input_mapping(abstract_input_file, mapped_input_taskfiles, parents)
    #         yield abstract_input_file, mapped_input_taskfiles
    #
    # def _map_input(self, abstract_input_file, parents):
    #     for p in parents:
    #         for tf in _find(p.output_files + p.forwarded_inputs, abstract_input_file, error_if_missing=False):
    #             yield tf

    def _generate_task(self, stage, parents, default_drm):
        assert self.out is not None
        self.output_dir = str_format(self.out, self.tags, '%s.output_dir' % self)
        # self.output_dir = os.path.join(stage.execution.output_dir, self.output_dir)
        d = {attr: getattr(self, attr) for attr in ['mem_req', 'time_req', 'cpu_req', 'must_succeed']}
        d['drm'] = 'local' if self.drm is not None else default_drm

        # Validation
        f = lambda ifa: ifa.taskfile
        for tf, group_of_ifas in it.groupby(sorted(ifas, key=f), f):
            group_of_ifas = list(group_of_ifas)
            if len(group_of_ifas) > 1:
                error('An input file mapped to multiple AbstractInputFiles for %s' % self, dict(
                    TaskFiles=tf
                ))

        task = Task(stage=stage, tags=self.tags,  parents=parents, output_dir=self.output_dir,
                    input_files=self.input_arg_map.values(),
                    output_files=self.output_arg_map.values()
                    **d)
        task.skip_profile = self.skip_profile

        # inputs = unpack_taskfiles_with_cardinality_1(aif_2_input_taskfiles).values()

        task.tool = self
        return task



    def _cmd(self, possible_input_taskfiles, output_taskfiles, task):
        argspec = getargspec(self.cmd)
        self.task = task

        def get_params():
            for k in argspec.args:
                if k in self.tags:
                    yield k, self.tags[k]

        params = dict(get_params())

        # params = {k: v
        #           for k, v in self.tags.items()
        #           if k in argspec.args
        #           if k not in self.input_arg_map and k not in self.output_arg_map}

        def validate_params():
            ndefaults = len(argspec.defaults) if argspec.defaults else 0
            for arg in argspec.args[1:-1 * ndefaults]:
                if arg not in params:
                    raise AttributeError(
                        '%s.cmd() requires the parameter `%s`, are you missing a tag?  Either provide a default in the cmd() '
                        'method signature, or pass a value for `%s` with a tag' % (self, arg, arg))

        validate_params()

        def get_input_map():
            for input_name, aif in self.input_arg_map.iteritems():
                if input_name in params:
                    # did user manually set input path?
                    # TODO check that this is a TaskFile?  Probably not..
                    yield input_name, params[input_name]
                else:
                    # find the input automatically
                    input_taskfiles = list(_find(possible_input_taskfiles, aif, error_if_missing=True))
                    input_taskfile_or_input_taskfiles = unpack_if_cardinality_1(aif, input_taskfiles)
                    yield input_name, input_taskfile_or_input_taskfiles

        input_map = dict(get_input_map())

        outputs = sorted(output_taskfiles, key=lambda tf: tf.order)
        output_map = dict(zip(self.output_arg_map.iterkeys(), outputs))

        kwargs = dict()
        kwargs.update(input_map)
        kwargs.update(output_map)
        kwargs.update(params)

        out = self.cmd(**kwargs)

        assert isinstance(out, basestring), '%s.cmd did not return a str' % self
        out = re.sub('<TaskFile\[(.*?)\] .+?:(.+?)>', lambda m: m.group(2), out)
        return out  # strip_lines(out)

    def before_cmd(self):
        task = self.task
        o = '#!/bin/bash\n' \
            'set -e\n' \
            'set -o pipefail\n' \
            'cd %s\n' % task.execution.output_dir

        if task.output_dir:
            o += 'mkdir -p %s\n' % task.output_dir

        o += "\n"

        return o


    def after_cmd(self):
        return ''

    def cmd(self, **kwargs):
        """
        Constructs the command string.  Lines will be .strip()ed.
        :param dict kwargs:  Inputs and Outputs (which have AbstractInputFile and AbstractOutputFile defaults) and parameters which are passed via tags.
        :rtype: str
        :returns: The text to write into the shell script that gets executed
        """
        raise NotImplementedError("{0}.cmd is not implemented.".format(self.__class__.__name__))

    def _generate_command(self, task):
        """
        Generates the command
        """
        cmd = self._cmd(task.input_files, task.output_files, task)
        if cmd == NOOP:
            return NOOP
        return self.before_cmd() + self._cmd(task.input_files, task.output_files, task) + self.after_cmd()

    def __repr__(self):
        return '<Tool[%s] %s %s>' % (id(self), self.name, self.tags)


class Tool_old(Tool):
    """
    Old input/output specification.  Deprecated and will be removed.
    """
    api_version = 1


from collections import namedtuple


class InputSource(namedtuple('InputSource', ['path', 'name', 'format'])):
    def __init__(self, path, name=None, format=None):
        basename = os.path.basename(path)
        if name is None:
            name = os.path.splitext(basename)[0]
        if format is None:
            format = os.path.splitext(basename)[-1][1:]  # remove the '.'

        super(InputSource, self).__init__(path, name, format)


def set_default_name_format(path, name=None, format=None):
    default_name, default_ext = os.path.splitext(os.path.basename(path))

    if name is None:
        name = default_name
    if format is None:
        format = default_ext[1:]

    return name, format


class Input(Tool):
    """
    A NOOP Task who's output_files contain a *single* file that already exists on the filesystem.


    >>> Input(path_to_file,tags={'key':'val'})
    >>> Input(path=path_to_file, name='myfile',format='txt',tags={'key':'val'})
    """

    name = 'Load_Input_Files'
    cpu_req = 0

    def __init__(self, path, name=None, format=None, tags=None, *args, **kwargs):
        """
        :param str path: the path to the input file
        :param str name: the name or keyword for the input file.  defaults to whatever format is set to.
        :param str format: the format of the input file.  Defaults to the value in `name`
        :param dict tags: tags for the task that will be generated
        """

        # path = _abs(path)
        if tags is None:
            tags = dict()

        name, format = set_default_name_format(path, name, format)

        super(Input, self).__init__(tags=tags, *args, **kwargs)
        self.load_sources.append(InputSource(path, name, format))

    def cmd(self, *args, **kwargs):
        return NOOP


class Inputs(Tool):
    """
    Same as :class:`Input`, but loads multiple input files.

    >>> Inputs([('name1','txt','/path/to/input'), ('name2','gz','/path/to/input2')], tags={'key':'val'})
    "root_path   name = 'Load_Input_Files'
    """
    name = 'Load_Input_Files'
    cpu_req = 0

    def __init__(self, inputs, tags=None, *args, **kwargs):
        """
        :param list inputs: a list of tuples that are (path, name, format)
        :param dict tags:
        """
        # self.NOOP = True
        if tags is None:
            tags = dict()

        super(Inputs, self).__init__(tags=tags, *args, **kwargs)
        for path, name, fmt in inputs:
            name, fmt = set_default_name_format(path, name, fmt)
            self.load_sources.append(InputSource(path, name, fmt))

    def cmd(self, *args, **kwargs):
        return NOOP


def _abs(path):
    path2 = os.path.abspath(os.path.expanduser(path))
    assert os.path.exists(path2), '%s path does not exist' % path2
    return path2


def unpack_taskfiles_with_cardinality_1(odict):
    new = odict.copy()
    for aif, taskfiles in odict.items():
        op, number = parse_aif_cardinality(aif.n)
        if op in ['=', '=='] and number == 1:
            new[aif] = taskfiles[0]
        else:
            new[aif] = taskfiles
    return new


def unpack_if_cardinality_1(aif, taskfiles):
    op, number = parse_aif_cardinality(aif.n)
    if op in ['=', '=='] and number == 1:
        return taskfiles[0]
    else:
        return taskfiles


def _find(filenames, regex, error_if_missing=False):
    found = False
    for filename in filenames:
        if re.search(regex, filename):
            yield filename
            found = True

    if not found and error_if_missing:
        raise ValueError, 'No taskfile found for %s' % regex
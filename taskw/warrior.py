""" Code to interact with taskwarrior

This module contains an abstract base class and two different implementations
for interacting with taskwarrior:  TaskWarrior and TaskWarriorExperimental.

"""

import abc
import codecs
from distutils.version import LooseVersion
import os
import re
import sys
import time
import uuid
import subprocess
import json
import pprint

import taskw.utils

import six
from six import with_metaclass
from six.moves import filter
from six.moves import map
from six.moves import zip


open = lambda fname, mode: codecs.open(fname, mode, "utf-8")


class TaskWarriorBase(with_metaclass(abc.ABCMeta, object)):
    """ The task warrior

    Really though, a python object with methods allowing you to interact
    with a taskwarrior database.
    """

    def __init__(self, config_filename="~/.taskrc"):
        self.config_filename = config_filename
        self.config = TaskWarriorBase.load_config(config_filename)

    def _stub_task(self, description, tags=None, **kw):
        """ Given a description, stub out a task dict. """

        # If whitespace is not removed here, TW will do it when we pass the
        # task to it.
        task = {"description": description.strip()}

        # Allow passing "tags" in as part of kw.
        if 'tags' in kw and tags is None:
            task['tags'] = tags
            del(kw['tags'])

        if tags is not None:
            task['tags'] = tags

        task.update(kw)

        # Only UNIX timestamps are currently supported.
        if 'due' in kw:
            task['due'] = str(task['due'])

        return task

    def _extract_annotations_from_task(self, task):
        """ Removes annotations from a task and returns a list of annotations
        """
        annotations = list()
        for key in task.keys():
            if key.startswith('annotation_'):
                annotations.append(task[key])
                del(task[key])
        return annotations

    @abc.abstractmethod
    def load_tasks(self, command='all'):
        """ Load all tasks.

        Similar to TaskWarrior, a specific command may be specified:

            all       - a list of all issues
            pending   - a list of all pending issues
            completed - a list of all completed issues

        By default, the 'all' command is run.

        >>> w = Warrior()
        >>> tasks = w.load_tasks()
        >>> tasks.keys()
        ['completed', 'pending']
        >>> type(tasks['pending'])
        <type 'list'>
        >>> type(tasks['pending'][0])
        <type 'dict'>
        """

    @abc.abstractmethod
    def task_add(self, description, tags=None, **kw):
        """ Add a new task.

        Takes any of the keywords allowed by taskwarrior like proj or prior.
        """
        pass

    @abc.abstractmethod
    def task_done(self, **kw):
        pass

    @abc.abstractmethod
    def _load_task(self, **kw):
        pass

    @abc.abstractmethod
    def task_update(self, task):
        pass

    @abc.abstractmethod
    def get_task(self, **kw):
        pass

    def filter_by(self, func):
        tasks = self.load_tasks()
        filtered = filter(func, tasks)
        return filtered

    @classmethod
    def load_config(self, config_filename="~/.taskrc"):
        """ Load ~/.taskrc into a python dict

        >>> config = TaskWarrior.load_config()
        >>> config['data']['location']
        '/home/threebean/.task'
        >>> config['_forcecolor']
        'yes'

        """

        with open(os.path.expanduser(config_filename), 'r') as f:
            lines = f.readlines()

        _usable = lambda l: not(l.startswith('#') or l.strip() == '')
        lines = filter(_usable, lines)

        def _build_config(key, value, d):
            """ Called recursively to split up keys """
            pieces = key.split('.', 1)
            if len(pieces) == 1:
                d[pieces[0]] = value.strip()
            else:
                d[pieces[0]] = _build_config(pieces[1], value, {})

            return d

        d = {}
        for line in lines:
            if '=' not in line:
                continue

            key, value = line.split('=', 1)
            d = _build_config(key, value, d)

        # Set a default data location if one is not specified.
        if d.get('data') is None:
            d['data'] = {}

        if d['data'].get('location') is None:
            d['data']['location'] = os.path.expanduser("~/.task/")

        return d


class TaskWarrior(TaskWarriorBase):
    """ Interacts with taskwarrior by directly manipulating the ~/.task/ db.

    Currently this is the supported implementation, but will be phased out in
    time due to taskwarrior's guidelines:  http://bit.ly/16I9VN4

    See https://github.com/ralphbean/taskw/pull/15 for discussion.
    """

    def sync(self):
        raise NotImplementedError(
            "You must use TaskWarriorExperimental to use 'sync'"
        )

    def load_tasks(self, command='all'):
        def _load_tasks(filename):
            filename = os.path.join(self.config['data']['location'], filename)
            filename = os.path.expanduser(filename)
            with open(filename, 'r') as f:
                lines = f.readlines()

            return list(map(taskw.utils.decode_task, lines))

        return dict(
            (db, _load_tasks(_DataFile.filename(db)))
            for db in _Command.files(command)
        )

    def get_task(self, **kw):
        line, task = self._load_task(**kw)

        id = None
        # The ID going back only makes sense if the task is pending.
        if _TaskStatus.is_pending(task['status']):
            id = line

        return id, task

    def _load_task(self, **kw):
        valid_keys = set(['id', 'uuid', 'description'])
        id_keys = valid_keys.intersection(kw.keys())

        if len(id_keys) != 1:
            raise KeyError("Only 1 ID keyword argument may be specified")

        key = list(id_keys)[0]
        if key not in valid_keys:
            raise KeyError("Argument must be one of %r" % valid_keys)

        line = None
        task = dict()

        # If the key is an id, assume the task is pending (completed tasks don't have IDs).
        if key == 'id':
            tasks = self.load_tasks(command=_TaskStatus.PENDING)
            line = kw[key]

            if len(tasks[_TaskStatus.PENDING]) >= line:
                task = tasks[_TaskStatus.PENDING][line - 1]

        else:
            # Search all tasks for the specified key.
            tasks = self.load_tasks(command=_Command.ALL)

            matching = list(filter(
                lambda t: t.get(key, None) == kw[key],
                sum(tasks.values(), [])
            ))

            if matching:
                task = matching[0]
                line = tasks[_TaskStatus.to_file(task['status'])].index(task) + 1

        return line, task

    def task_add(self, description, tags=None, **kw):
        """ Add a new task.

        Takes any of the keywords allowed by taskwarrior like proj or prior.
        """

        task = self._stub_task(description, tags, **kw)

        task['status'] = _TaskStatus.PENDING

        # TODO -- check only valid keywords

        if not 'entry' in task:
            task['entry'] = str(int(time.time()))

        if not 'uuid' in task:
            task['uuid'] = str(uuid.uuid4())

        id = self._task_add(task, _TaskStatus.PENDING)
        task['id'] = id
        return task

    def task_done(self, **kw):
        """
        Marks a pending task as done, optionally specifying a completion
        date with the 'end' argument.
        """
        def validate(task):
            if not _TaskStatus.is_pending(task['status']):
                raise ValueError("Task is not pending.")

        return self._task_change_status(_TaskStatus.COMPLETED, validate, **kw)

    def task_update(self, task):
        line, _task = self._load_task(uuid=task['uuid'])

        if 'id' in task:
            del task['id']

        _task.update(task)
        self._task_replace(line, _TaskStatus.to_file(task['status']), _task)
        return line, _task

    def task_delete(self, **kw):
        """
        Marks a task as deleted, optionally specifying a completion
        date with the 'end' argument.
        """
        def validate(task):
            if task['status'] == _TaskStatus.DELETED:
                raise ValueError("Task is already deleted.")

        return self._task_change_status(_TaskStatus.DELETED, validate, **kw)

    def _task_replace(self, id, category, task):
        def modification(lines):
            lines[id - 1] = taskw.utils.encode_task(task)
            return lines

        # FIXME write to undo.data
        self._apply_modification(id, category, modification)

    def _task_remove(self, id, category):
        def modification(lines):
            del lines[id - 1]
            return lines

        # FIXME write to undo.data
        self._apply_modification(id, category, modification)

    def _apply_modification(self, id, category, modification):
        location = self.config['data']['location']
        filename = _DataFile.filename(category)
        filename = os.path.join(self.config['data']['location'], filename)
        filename = os.path.expanduser(filename)

        with open(filename, "r") as f:
            lines = f.readlines()

        lines = modification(lines)

        with open(filename, "w") as f:
            f.writelines(lines)

    def _task_add(self, task, category):
        location = self.config['data']['location']
        location = os.path.expanduser(location)
        filename = category + '.data'

        # Append the task
        with open(os.path.join(location, filename), "a") as f:
            f.writelines([taskw.utils.encode_task(task)])

        # FIXME - this gets written when a task is completed.  incorrect.
        # Add to undo.data
        with open(os.path.join(location, 'undo.data'), "a") as f:
            f.write("time %s\n" % str(int(time.time())))
            f.write("new %s" % taskw.utils.encode_task(task))
            f.write("---\n")

        with open(os.path.join(location, filename), "r") as f:
            # The 'id' of this latest added task.
            return len(f.readlines())

    def _task_change_status(self, status, validation, **kw):
        line, task = self._load_task(**kw)
        validation(task)
        original_status = task['status']

        task['status'] = status
        task['end'] = kw.get('end') or str(int(time.time()))

        self._task_add(task, _TaskStatus.to_file(status))
        self._task_remove(line, _TaskStatus.to_file(original_status))
        return task


class TaskWarriorExperimental(TaskWarriorBase):
    """ Interacts with taskwarrior by invoking shell commands.

    This is currently experimental and is not necessarily stable.  Please help
    us test and report any issues.

    Some day this will become the primary supported implementation due to
    taskwarrior's guidelines:  http://bit.ly/16I9VN4

    See https://github.com/ralphbean/taskw/pull/15 for discussion.
    """
    def _execute(self, *args):
        """ Execute a given taskwarrior command with arguments

        Returns a 2-tuple of stdout and stderr (respectively).

        """
        command = [
            'task',
            'rc:%s' % self.config_filename,
            'rc.json.array=TRUE',
            'rc.verbose=nothing',
        ] + [six.text_type(arg) for arg in args]
        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        ).communicate()

    def _get_json(self, *args):
        try:
            return json.loads(
                self._execute(*args)[0].decode(sys.getdefaultencoding())
            )
        except ValueError:
            # An empty string causes json.loads to raise a ValueError
            return None

    @classmethod
    def can_use(cls):
        """ Returns true if runtime requirements of experimental mode are met
        """
        try:
            return cls.get_version() > LooseVersion('2')
        except OSError:
            # OSError is raised if subprocess.Popen fails to find
            # the executable.
            return False

    @classmethod
    def get_version(cls):
        taskwarrior_version = subprocess.Popen(
            ['task', '--version'],
            stdout=subprocess.PIPE
        ).communicate()[0]
        return LooseVersion(taskwarrior_version.decode())

    def sync(self):
        if self.get_version() < LooseVersion('2.3'):
            raise UnsupportedVersionException(
                "'sync' requires version 2.3 of taskwarrior or later."
            )
        subprocess.Popen([
            'task',
            'rc:%s' % self.config_filename,
            'sync',
        ])

    def load_tasks(self, statuses=None, **kw):
        """ Returns a dictionary of tasks for a list of statuses."""
        if statuses is None:
            statuses = ['pending', 'completed']

        tasks = {}

        for status in statuses:
            tasks[status] = self._get_json(
                'status:%s' % status,
                'export'
            )

        return tasks

    def get_task(self, **kw):
        task = dict()
        task_id = None
        task_id, task = self._load_task(**kw)
        id = None
        # The ID going back only makes sense if the task is pending.
        if 'status' in task:
            if _TaskStatus.is_pending(task['status']):
                id = task_id

        return id, task

    def _load_task(self, **kwargs):
        if len(kwargs) > 1:
            raise KeyError(
                "Only one keyword argument may be specified"
            )

        search = []
        for key, value in six.iteritems(kwargs):
            if key not in ['id', 'uuid', 'description']:
                search.append(
                    '%s:%s' % (
                        key,
                        value,
                    )
                )
            elif key == 'description' and '(bw)' in value:
                search.append(
                    value[4:]
                )
            else:
                search = [value]

        task = self._get_json('export', *search)

        if task:
            if isinstance(task, list):
                # Multiple items returned from search, return just the 1st
                task = task[0]
            return task['id'], task

        return None, dict()

    def task_add(self, description, tags=None, **kw):
        """ Add a new task.

        Takes any of the keywords allowed by taskwarrior like proj or prior.
        """

        task = self._stub_task(description, tags, **kw)

        # Check if there are annotations, if so remove them from the
        # task and add them after we've added the task.
        annotations = self._extract_annotations_from_task(task)

        self._execute(
            'add',
            taskw.utils.encode_task_experimental(task),
        )
        id, added_task = self.get_task(description=task['description'])

        # Check if 'uuid' is in the task we just added.
        if not 'uuid' in added_task:
            raise KeyError('No uuid! uh oh.')
        if annotations and 'uuid' in added_task:
            for annotation in annotations:
                self.task_annotate(added_task, annotation)
        id, added_task = self.get_task(uuid=added_task[six.u('uuid')])
        return added_task

    def task_annotate(self, task, annotation):
        """ Annotates a task. """
        self._execute(
            task['uuid'],
            'annotate',
            annotation
        )
        id, annotated_task = self.get_task(uuid=task[six.u('uuid')])
        return annotated_task

    def task_denotate(self, task, annotation):
        """ Removes an annotation from a task. """
        self._execute(
            task['uuid'],
            'denotate',
            annotation
        )
        id, denotated_task = self.get_task(uuid=task[six.u('uuid')])
        return denotated_task

    def task_done(self, **kw):
        if not kw:
            raise KeyError('No key was passed.')
        id, task = self.get_task(**kw)

        self._execute(id, 'do')
        return self.get_task(uuid=task['uuid'])

    def task_update(self, task):
        if 'uuid' not in task:
            raise KeyError('Task must have a UUID.')
        id, _task = self.get_task(uuid=task['uuid'])

        if 'id' in task:
            del task['id']

        _task.update(task)

        # Unset task attributes that should not be updated
        task_to_modify = _task
        del task_to_modify['uuid']
        del task_to_modify['id']

        # Check if there are annotations, if so, look if they are
        # in the existing task, otherwise annotate the task to add them.
        new_annotations = self._extract_annotations_from_task(task)
        existing_annotations = \
            self._extract_annotations_from_task(task_to_modify)

        if 'annotations' in task_to_modify:
            del task_to_modify['annotations']

        modification = taskw.utils.encode_task_experimental(task_to_modify)
        self._execute(task['uuid'], 'modify', modification)

        # If there are no existing annotations, add the new ones
        if existing_annotations is None:
            for annotation in new_annotations:
                self.task_annotate(task_to_modify, annotation)

        # If there are existing annotations and new annotations, add only
        # the new annotations
        if existing_annotations is not None and new_annotations is not None:
            for annotation in new_annotations:
                if annotation not in existing_annotations:
                    self.task_annotate(task_to_modify, annotation)

        return id, _task

    def task_info(self, **kw):
        id, task = self.get_task(**kw)
        self._get_json('info', id)
        out, err = info.communicate()
        if err:
            return err
        return out


class _DataFile(object):
    """ Encapsulates data file names. """
    PENDING = 'pending'
    COMPLETED = 'completed'

    @classmethod
    def filename(cls, name):
        return "%s.data" % name


class _Command(object):
    """ Encapsulates available commands. """
    PENDING = 'pending'
    COMPLETED = 'completed'
    ALL = 'all'

    @classmethod
    def files(cls, command):
        known_commands = {
                _Command.PENDING : [_DataFile.PENDING],
                _Command.COMPLETED : [_DataFile.COMPLETED],
                _Command.ALL : [_DataFile.PENDING, _DataFile.COMPLETED]
                }

        if not command in known_commands:
            raise ValueError("Unknown command, %s. Command must be one of %s." %
                    (command, known_commands.keys()))

        return known_commands[command]


class _TaskStatus(object):
    """ Encapsulates task status values. """
    PENDING = 'pending'
    COMPLETED = 'completed'
    DELETED = 'deleted'
    WAITING = 'waiting'

    @classmethod
    def is_pending(cls, status):
        """ Identifies if the specified status is a 'pending' state. """
        return status == _TaskStatus.PENDING or status == _TaskStatus.WAITING

    @classmethod
    def to_file(cls, status):
        """ Returns the file in which this task is stored. """
        return {
                _TaskStatus.PENDING : _DataFile.PENDING,
                _TaskStatus.WAITING : _DataFile.PENDING,
                _TaskStatus.COMPLETED : _DataFile.COMPLETED,
                _TaskStatus.DELETED : _DataFile.COMPLETED
        }[status]


class UnsupportedVersionException(object):
    pass

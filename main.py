#!/usr/bin/env python3
import functools
import os
import threading
from typing import Mapping, Optional

from docker import from_env as docker_from_env
from docker.models.containers import Container as DockerContainer

LABEL = os.environ['L2F_LABEL']
FILENAME_FORMAT = os.environ.get('L2F_FILENAME_FORMAT') or '{name}'
CONTAINER_TO_RESTART = os.environ.get('L2F_CONTAINER_TO_RESTART') or None
END_WITH_NEWLINE = os.environ.get('L2F_END_WITH_NEWLINE') or 'true'


class Template:
    def __init__(self, format_string: str):
        components = self.components = []

        in_bracket, span = False, ''
        for ch in format_string:
            if ch == '{':
                if in_bracket:
                    components.append((0, ch))
                    in_bracket = False
                else:
                    components.append((0, span))
                    in_bracket, span = True, ''

            elif ch == '}':
                if in_bracket:
                    key, _, def_val = span.partition(':')
                    components.append((1, (key, def_val)))
                    in_bracket, span = False, ''
                else:
                    span += ch

            else:
                span += ch

        components.append((0, (in_bracket and '{' or '') + span))

    def substitute(self, mapping: Mapping[str, str]) -> str:
        return ''.join((mapping.get(s[0]) or s[1] if t else s) for t, s in self.components)


def debounce(interval):
    def decorator(func):
        @functools.wraps(func)
        def debounced(*args, **kwargs):
            if hasattr(debounced, 'timer'):
                debounced.timer.cancel()

            debounced.timer = threading.Timer(
                interval, lambda: func(*args, **kwargs))
            debounced.timer.start()

        return debounced

    return decorator


docker_client = docker_from_env()
filename_template = Template(FILENAME_FORMAT)
container_to_restart = docker_client.containers.get(CONTAINER_TO_RESTART) \
    if CONTAINER_TO_RESTART else None


def get_attrs(_container: DockerContainer) -> Mapping[str, str]:
    _attrs = dict(name=container.name, image=container.image.tags[0])
    _attrs.update(container.labels)
    return _attrs


def get_filename(_attrs: Mapping[str, str]) -> str:
    if not (_filename := _attrs.get(f'{LABEL}.filename')):
        _filename = filename_template.substitute(_attrs)
    return _filename


def create_config(_attrs: Mapping[str, str], _filename: Optional[str] = None):
    if not _filename: _filename = get_filename(_attrs)
    dirname = os.path.dirname(_filename) or '.'

    os.makedirs(dirname, exist_ok=True)
    with open(_filename, mode='w') as fp:
        end_with_newline = _attrs.get(f'{LABEL}.end_with_newline')
        end_with_newline = end_with_newline.lower() in ('1', 'true', 'yes', 'on') \
            if end_with_newline else END_WITH_NEWLINE

        fp.write(_attrs[LABEL])
        if end_with_newline:
            fp.write('\n')


def restart_container_immediately():
    container_to_restart and container_to_restart.restart()


@debounce(3)
def try_to_restart_container():
    restart_container_immediately()


if __name__ == '__main__':
    for container in docker_client.containers.list(filters={
        'status': ['running'],
        'label': [LABEL]
    }):
        create_config(get_attrs(container))
        try_to_restart_container()

    try:
        for event in docker_client.events(decode=True, filters={
            'event': ['start', 'unpause', 'pause', 'die'],
            'type': 'container',
            'label': [LABEL]
        }):
            action, actor = event['Action'], event['Actor']
            attrs = actor['Attributes']

            if action in ('start', 'unpause'):
                try:
                    create_config(attrs)
                    try_to_restart_container()
                except IOError as ex:
                    print(ex)

            elif action in ('pause', 'die'):
                try:
                    os.remove(get_filename(attrs))
                    try_to_restart_container()
                except FileNotFoundError:
                    pass

    except KeyboardInterrupt:
        for container in docker_client.containers.list(filters={'label': [LABEL]}):
            try:
                os.remove(get_filename(get_attrs(container)))
            except FileNotFoundError:
                pass

        restart_container_immediately()

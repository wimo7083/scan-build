# -*- coding: utf-8 -*-
#                     The LLVM Compiler Infrastructure
#
# This file is distributed under the University of Illinois Open Source
# License. See LICENSE.TXT for details.
""" This module is responsible to capture the compiler invocation of any
build process. The result of that should be a compilation database.

This implementation is using the LD_PRELOAD or DYLD_INSERT_LIBRARIES
mechanisms provided by the dynamic linker. The related library is implemented
in C language and can be found under 'libear' directory.

The 'libear' library is capturing all child process creation and logging the
relevant information about it into separate files in a specified directory.
The parameter of this process is the output directory name, where the report
files shall be placed. This parameter is passed as an environment variable.

The module also implements compiler wrappers to intercept the compiler calls.

The module implements the build command execution and the post-processing of
the output files, which will condensates into a compilation database. """

import sys
import os
import os.path
import re
import itertools
import json
import glob
import argparse
import logging
import subprocess
from libear import ear_library, TemporaryDirectory
from libscanbuild import duplicate_check, tempdir, initialize_logging
from libscanbuild import logging_internal_error
from libscanbuild.command import Action, classify_parameters
from libscanbuild.shell import encode, decode

__all__ = ['capture', 'main', 'wrapper']

GS = chr(0x1d)
RS = chr(0x1e)
US = chr(0x1f)


def main(wrappers_dir):
    """ Entry point for 'intercept-build' command. """

    try:
        parser = create_parser()
        args = parser.parse_args()

        initialize_logging(args.verbose)
        logging.debug('Parsed arguments: %s', args)

        if not args.build:
            parser.print_help()
            return 0

        return capture(args, wrappers_dir)
    except KeyboardInterrupt:
        return 1
    except Exception:
        logging_internal_error(args.verbose)
        return 127


def capture(args, wrappers_dir):
    """ The entry point of build command interception. """

    def post_processing(commands):
        """ To make a compilation database, it needs to filter out commands
        which are not compiler calls. Needs to find the source file name
        from the arguments. And do shell escaping on the command.

        To support incremental builds, it is desired to read elements from
        an existing compilation database from a previous run. These elemets
        shall be merged with the new elements. """

        # create entries from the current run
        current = itertools.chain.from_iterable(
            # creates a sequence of entry generators from an exec,
            # but filter out non compiler calls before.
            (format_entry(x) for x in commands if is_compiler_call(x)))
        # read entries from previous run
        if 'append' in args and args.append and os.path.exists(args.cdb):
            with open(args.cdb) as handle:
                previous = iter(json.load(handle))
        else:
            previous = iter([])
        # filter out duplicate entries from both
        duplicate = duplicate_check(entry_hash)
        return (entry
                for entry in itertools.chain(previous, current)
                if os.path.exists(entry['file']) and not duplicate(entry))

    with TemporaryDirectory(prefix='intercept-build', dir=tempdir()) as tmpdir:
        # run the build command
        environment = setup_environment(args, tmpdir, wrappers_dir)
        logging.debug('run build in environment: %s', environment)
        exit_code = subprocess.call(args.build, env=environment)
        logging.info('build finished with exit code: %d', exit_code)
        # read the intercepted exec calls
        commands = itertools.chain.from_iterable(
            parse_exec_trace(os.path.join(tmpdir, filename))
            for filename in sorted(glob.iglob(os.path.join(tmpdir, '*.cmd'))))
        # do post processing only if that was requested
        if 'raw_entries' not in args or not args.raw_entries:
            entries = post_processing(commands)
        else:
            entries = commands
        # dump the compilation database
        with open(args.cdb, 'w+') as handle:
            json.dump(list(entries), handle, sort_keys=True, indent=4)
        return exit_code


def setup_environment(args, destination, wrappers_dir):
    """ Sets up the environment for the build command.

    It sets the required environment variables and execute the given command.
    The exec calls will be logged by the 'libear' preloaded library or by the
    'wrapper' programs. """

    c_compiler = args.cc if 'cc' in args else 'cc'
    cxx_compiler = args.cxx if 'cxx' in args else 'c++'

    ear_library_path = None if args.override_compiler else \
        ear_library(c_compiler, destination)

    environment = dict(os.environ)
    environment.update({'INTERCEPT_BUILD_TARGET_DIR': destination})

    if not ear_library_path:
        logging.debug('intercept gonna use compiler wrappers')
        environment.update({
            'CC': os.path.join(wrappers_dir, 'intercept-cc'),
            'CXX': os.path.join(wrappers_dir, 'intercept-cxx'),
            'INTERCEPT_BUILD_CC': c_compiler,
            'INTERCEPT_BUILD_CXX': cxx_compiler,
            'INTERCEPT_BUILD_VERBOSE': 'DEBUG' if args.verbose > 2 else 'INFO'
        })
    elif sys.platform == 'darwin':
        logging.debug('intercept gonna preload libear on OSX')
        environment.update({
            'DYLD_INSERT_LIBRARIES': ear_library_path,
            'DYLD_FORCE_FLAT_NAMESPACE': '1'
        })
    else:
        logging.debug('intercept gonna preload libear on UNIX')
        environment.update({'LD_PRELOAD': ear_library_path})

    return environment


def wrapper(cplusplus):
    """ Entry point for `intercept-cc` and `intercept-c++` compiler wrappers.

    It does generate execution report into target directory. And execute
    the wrapped compilation with the real compiler. The parameters for
    report and execution are from environment variables.

    Those parameters which for 'libear' library can't have meaningful
    values are faked. """

    # initialize wrapper logging
    logging.basicConfig(format='intercept: %(levelname)s: %(message)s',
                        level=os.getenv('INTERCEPT_BUILD_VERBOSE', 'INFO'))
    # write report
    try:
        target_dir = os.getenv('INTERCEPT_BUILD_TARGET_DIR')
        if not target_dir:
            raise UserWarning('exec report target directory not found')
        pid = str(os.getpid())
        target_file = os.path.join(target_dir, pid + '.cmd')
        logging.debug('writing exec report to: %s', target_file)
        with open(target_file, 'ab') as handler:
            working_dir = os.getcwd()
            command = US.join(sys.argv) + US
            content = RS.join([pid, pid, 'wrapper', working_dir, command]) + GS
            handler.write(content.encode('utf-8'))
    except IOError:
        logging.exception('writing exec report failed')
    except UserWarning as warning:
        logging.warning(warning)
    # execute with real compiler
    compiler = os.getenv('INTERCEPT_BUILD_CXX', 'c++') if cplusplus \
        else os.getenv('INTERCEPT_BUILD_CC', 'cc')
    compilation = [compiler] + sys.argv[1:]
    logging.debug('execute compiler: %s', compilation)
    return subprocess.call(compilation)


def parse_exec_trace(filename):
    """ Parse the file generated by the 'libear' preloaded library.

    Given filename points to a file which contains the basic report
    generated by the interception library or wrapper command. A single
    report file _might_ contain multiple process creation info. """

    logging.debug('parse exec trace file: %s', filename)
    with open(filename, 'r') as handler:
        content = handler.read()
        for group in filter(bool, content.split(GS)):
            records = group.split(RS)
            yield {
                'pid': records[0],
                'ppid': records[1],
                'function': records[2],
                'directory': records[3],
                'command': records[4].split(US)[:-1]
            }


def format_entry(entry):
    """ Generate the desired fields for compilation database entries. """

    def abspath(cwd, name):
        """ Create normalized absolute path from input filename. """
        fullname = name if os.path.isabs(name) else os.path.join(cwd, name)
        return os.path.normpath(fullname)

    logging.debug('format this command: %s', entry['command'])
    atoms = classify_parameters(entry['command'])
    if atoms['action'] <= Action.Compile:
        for source in atoms['files']:
            compiler = 'c++' if atoms['c++'] else 'cc'
            output = ['-o', atoms['output']] if atoms.get('output') else []
            command = [compiler, '-c'] + atoms['compile_options'] + output + \
                      [source]
            logging.debug('formated as: %s', command)
            yield {
                'directory': entry['directory'],
                'command': encode(command),
                'file': abspath(entry['directory'], source)
            }


def is_compiler_call(entry):
    """ A predicate to decide the entry is a compiler call or not. """

    patterns = [
        re.compile(r'^([^/]*/)*intercept-c(c|\+\+)$'),
        re.compile(r'^([^/]*/)*c(c|\+\+)$'),
        re.compile(r'^([^/]*/)*([^-]*-)*[mg](cc|\+\+)(-\d+(\.\d+){0,2})?$'),
        re.compile(r'^([^/]*/)*([^-]*-)*clang(\+\+)?(-\d+(\.\d+){0,2})?$'),
        re.compile(r'^([^/]*/)*llvm-g(cc|\+\+)$'),
    ]
    executable = entry['command'][0]
    return any((pattern.match(executable) for pattern in patterns))


def entry_hash(entry):
    """ Implement unique hash method for compilation database entries. """

    # For faster lookup in set filename is reverted
    filename = entry['file'][::-1]
    # For faster lookup in set directory is reverted
    directory = entry['directory'][::-1]
    # On OS X the 'cc' and 'c++' compilers are wrappers for
    # 'clang' therefore both call would be logged. To avoid
    # this the hash does not contain the first word of the
    # command.
    command = ' '.join(decode(entry['command'])[1:])

    return '<>'.join([filename, directory, command])


def create_parser():
    """ Command line argument parser factory method. """

    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument(
        '--verbose', '-v',
        action='count',
        default=0,
        help="""Enable verbose output from '%(prog)s'. A second and third
                flag increases verbosity.""")
    parser.add_argument(
        '--cdb',
        metavar='<file>',
        default="compile_commands.json",
        help="""The JSON compilation database.""")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        '--append',
        action='store_true',
        help="""Append new entries to existing compilation database.""")
    group.add_argument(
        '--disable-filter', '-n',
        dest='raw_entries',
        action='store_true',
        help="""Intercepted child process creation calls (exec calls) are all
                logged to the output. The output is not a compilation database.
                This flag is for debug purposes.""")

    advanced = parser.add_argument_group('advanced options')
    advanced.add_argument(
        '--override-compiler',
        action='store_true',
        help="""Always resort to the compiler wrapper even when better
                intercept methods are available.""")
    advanced.add_argument(
        '--use-cc',
        metavar='<path>',
        dest='cc',
        default='cc',
        help="""When '%(prog)s' analyzes a project by interposing a compiler
                wrapper, which executes a real compiler for compilation and
                do other tasks (record the compiler invocation). Because of
                this interposing, '%(prog)s' does not know what compiler your
                project normally uses. Instead, it simply overrides the CC
                environment variable, and guesses your default compiler.

                If you need '%(prog)s' to use a specific compiler for
                *compilation* then you can use this option to specify a path
                to that compiler.""")
    advanced.add_argument(
        '--use-c++',
        metavar='<path>',
        dest='cxx',
        default='c++',
        help="""This is the same as "--use-cc" but for C++ code.""")

    parser.add_argument(
        dest='build',
        nargs=argparse.REMAINDER,
        help="""Command to run.""")

    return parser

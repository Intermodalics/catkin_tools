# Copyright 2014 Open Source Robotics Foundation, Inc.
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

import csv
import os
from hashlib import md5

from catkin_tools.argument_parsing import handle_make_arguments
from catkin_tools.common import mkdir_p
from catkin_tools.execution.io import CatkinTestResultsIOBufferProtocol
from catkin_tools.execution.jobs import Job
from catkin_tools.execution.stages import CommandStage
from catkin_tools.execution.stages import FunctionStage

from .cmake import copy_install_manifest
from .cmake import get_python_install_dir
from .commands.cmake import CMAKE_EXEC
from .commands.cmake import CMakeIOBufferProtocol
from .commands.cmake import CMakeMakeIOBufferProtocol
from .commands.cmake import CMakeMakeRunTestsIOBufferProtocol
from .commands.cmake import get_installed_files
from .commands.make import MAKE_EXEC
from .utils import copyfiles
from .utils import loadenv
from .utils import makedirs
from .utils import require_command
from .utils import rmfiles


def get_prebuild_package(build_space_abs, devel_space_abs, force):
    """This generates a minimal Catkin package used to generate Catkin
    environment setup files in a merged devel space.

    :param build_space_abs: The path to a merged build space
    :param devel_space_abs: The path to a merged devel space
    :param force: Overwrite files if they exist
    :returns: source directory path
    """

    # Get the path to the prebuild package
    prebuild_path = os.path.join(build_space_abs, 'catkin_tools_prebuild')
    if not os.path.exists(prebuild_path):
        mkdir_p(prebuild_path)

    # Create CMakeLists.txt file
    cmakelists_txt_path = os.path.join(prebuild_path, 'CMakeLists.txt')
    if force or not os.path.exists(cmakelists_txt_path):
        with open(cmakelists_txt_path, 'wb') as cmakelists_txt:
            cmakelists_txt.write(SETUP_PREBUILD_CMAKELISTS_TEMPLATE.encode('utf-8'))

    # Create package.xml file
    package_xml_path = os.path.join(prebuild_path, 'package.xml')
    if force or not os.path.exists(package_xml_path):
        with open(package_xml_path, 'wb') as package_xml:
            package_xml.write(SETUP_PREBUILD_PACKAGE_XML_TEMPLATE.encode('utf-8'))

    # Create CATKIN_IGNORE file because this package should not be found by catkin
    # This is only necessary when the build space is inside of the source space
    catkin_ignore_path = os.path.join(prebuild_path, 'CATKIN_IGNORE')
    open(catkin_ignore_path, 'wb').close()

    # Create the build directory for this package
    mkdir_p(os.path.join(build_space_abs, 'catkin_tools_prebuild'))

    return prebuild_path


def clean_linked_files(
        logger,
        event_queue,
        metadata_path,
        files_that_collide,
        files_to_clean,
        dry_run):
    """Removes a list of files and adjusts collision counts for colliding files.

    This function synchronizes access to the devel collisions file.

    :param metadata_path: absolute path to the general metadata directory
    :param files_that_collide: list of absolute paths to files that collide
    :param files_to_clean: list of absolute paths to files to clean
    :param dry_run: Perform a dry-run
    """

    # Get paths
    devel_collisions_file_path = os.path.join(metadata_path, 'devel_collisions.txt')

    # Map from dest files to number of collisions
    dest_collisions = dict()

    # Load destination collisions file
    if os.path.exists(devel_collisions_file_path):
        with open(devel_collisions_file_path, 'r') as collisions_file:
            collisions_reader = csv.reader(collisions_file, delimiter=' ', quotechar='"')
            dest_collisions = dict([(path, int(count)) for path, count in collisions_reader])

    # Add collisions
    for dest_file in files_that_collide:
        if dest_file in dest_collisions:
            dest_collisions[dest_file] += 1
        else:
            dest_collisions[dest_file] = 1

    # Remove files that no longer collide
    for dest_file in files_to_clean:
        # Get the collisions
        n_collisions = dest_collisions.get(dest_file, 0)

        # Check collisions
        if n_collisions == 0:
            logger.out('Unlinking: {}'.format(dest_file))
            # Remove this link
            if not dry_run:
                if os.path.exists(dest_file):
                    try:
                        os.unlink(dest_file)
                    except OSError:
                        logger.err('Could not unlink: {}'.format(dest_file))
                        raise
                    # Remove any non-empty directories containing this file
                    try:
                        os.removedirs(os.path.split(dest_file)[0])
                    except OSError:
                        pass
                else:
                    logger.out('Already unlinked: {}')

        # Update collisions
        if n_collisions > 1:
            # Decrement the dest collisions dict
            dest_collisions[dest_file] -= 1
        elif n_collisions == 1:
            # Remove it from the dest collisions dict
            del dest_collisions[dest_file]

    # Load destination collisions file
    if not dry_run:
        with open(devel_collisions_file_path, 'w') as collisions_file:
            collisions_writer = csv.writer(collisions_file, delimiter=' ', quotechar='"')
            for dest_file, count in dest_collisions.items():
                collisions_writer.writerow([dest_file, count])


def unlink_devel_products(
        logger,
        event_queue,
        devel_space_abs,
        private_devel_path,
        metadata_path,
        package_metadata_path,
        dry_run):
    """
    Remove all files listed in the devel manifest for the given package, as
    well as any empty directories containing those files.

    :param devel_space_abs: Path to a merged devel space.
    :param private_devel_path: Path to the private devel space
    :param metadata_path: Path to the directory containing the general metadata
    :param package_metadata_path: Path to the directory containing the package's
    catkin_tools metadata
    :param dry_run: Perform a dry-run
    """

    # Check paths
    if not os.path.exists(private_devel_path):
        logger.err('Warning: No private devel path found at `{}`'.format(private_devel_path))
        return 0

    devel_manifest_file_path = os.path.join(package_metadata_path, DEVEL_MANIFEST_FILENAME)
    if not os.path.exists(devel_manifest_file_path):
        logger.err('Error: No devel manifest found at `{}`'.format(devel_manifest_file_path))
        return 1

    # List of files to clean
    files_to_clean = []

    # Read in devel_manifest.txt
    with open(devel_manifest_file_path, 'r') as devel_manifest:
        devel_manifest.readline()
        manifest_reader = csv.reader(devel_manifest, delimiter=' ', quotechar='"')

        # Remove all listed symlinks and empty directories
        for source_file, dest_file in manifest_reader:
            if not os.path.exists(dest_file):
                logger.err("Warning: Dest file doesn't exist, so it can't be removed: " + dest_file)
            elif not os.path.islink(dest_file):
                logger.err("Error: Dest file isn't a symbolic link: " + dest_file)
                return -1
            elif False and os.path.realpath(dest_file) != source_file:
                logger.err("Error: Dest file isn't a symbolic link to the expected file: " + dest_file)
                return -1
            else:
                # Clean the file or decrement the collision count
                files_to_clean.append(dest_file)

    # Remove all listed symlinks and empty directories which have been removed
    # after this build, and update the collision file
    clean_linked_files(logger, event_queue, metadata_path, [], files_to_clean, dry_run)

    return 0


def link_devel_products(
        logger, event_queue,
        package,
        package_path,
        devel_manifest_path,
        source_devel_path,
        dest_devel_path,
        metadata_path,
        prebuild):
    """Link files from an isolated devel space into a merged one.

    This creates directories and symlinks in a merged devel space to a
    package's linked devel space.
    """

    # Create the devel manifest path if necessary
    mkdir_p(devel_manifest_path)

    # Construct manifest file path
    devel_manifest_file_path = os.path.join(devel_manifest_path, DEVEL_MANIFEST_FILENAME)

    # Pair of source/dest files or directories
    products = list()
    # List of files to clean
    files_to_clean = []
    # List of files that collide
    files_that_collide = []

    # Select the skiplist
    skiplist = DEVEL_LINK_PREBUILD_SKIPLIST if prebuild else DEVEL_LINK_SKIPLIST

    def should_skip_file(filename):
        # Skip files that are in the skiplist...
        if os.path.relpath(os.path.join(source_path, filename), source_devel_path) in skiplist:
            return True
        # ... or somewhere in a directory in the directory skip list
        for directory in os.path.relpath(os.path.join(source_path, filename), source_devel_path).split(os.path.sep):
            if directory in DEVEL_LINK_SKIP_DIRECTORIES:
                return True
        return False

    # Gather all of the files in the devel space
    for source_path, dirs, files in os.walk(source_devel_path):
        # compute destination path
        dest_path = os.path.join(dest_devel_path, os.path.relpath(source_path, source_devel_path))

        # create directories in the destination develspace
        for dirname in dirs:
            if dirname in DEVEL_LINK_SKIP_DIRECTORIES:
                continue

            source_dir = os.path.join(source_path, dirname)
            dest_dir = os.path.join(dest_path, dirname)

            if os.path.islink(source_dir):
                # Store the source/dest pair
                products.append((source_dir, dest_dir))

                if os.path.exists(dest_dir):
                    if os.path.realpath(dest_dir) != os.path.realpath(source_dir):
                        files_that_collide.append(dest_dir)
                    else:
                        logger.out('Linked: ({}, {})'.format(source_dir, dest_dir))
                else:
                    # Create a symlink
                    logger.out('Symlinking %s' % dest_dir)
                    try:
                        os.symlink(source_dir, dest_dir)
                    except OSError:
                        logger.err('Could not create symlink `{}` referencing `{}`'.format(dest_dir, source_dir))
                        raise
            else:
                if not os.path.exists(dest_dir):
                    # Create the dest directory if it doesn't exist
                    os.mkdir(dest_dir)
                elif not os.path.isdir(dest_dir):
                    logger.err('Error: Cannot create directory: {}'.format(dest_dir))
                    return -1

        # create symbolic links from the source to the dest
        for filename in files:

            # Don't link files on the skiplist
            if should_skip_file(filename):
                continue

            source_file = os.path.join(source_path, filename)
            dest_file = os.path.join(dest_path, filename)

            # Store the source/dest pair
            products.append((source_file, dest_file))

            # Check if the symlink exists
            if os.path.exists(dest_file):
                if os.path.realpath(dest_file) != os.path.realpath(source_file):
                    # Compute hashes for colliding files
                    source_hash = md5(open(os.path.realpath(source_file), "rb").read()).hexdigest()
                    dest_hash = md5(open(os.path.realpath(dest_file), "rb").read()).hexdigest()
                    # If the link links to a different file, report a warning and increment
                    # the collision counter for this path
                    if dest_hash != source_hash:
                        logger.err('Warning: Cannot symlink from %s to existing file %s' % (source_file, dest_file))
                        logger.err('Warning: Source hash: {}'.format(source_hash))
                        logger.err('Warning: Dest hash: {}'.format(dest_hash))
                    # Increment link collision counter
                    files_that_collide.append(dest_file)
                else:
                    logger.out('Linked: ({}, {})'.format(source_file, dest_file))
            else:
                # Create the symlink
                logger.out('Symlinking %s' % dest_file)
                try:
                    os.symlink(source_file, dest_file)
                except OSError:
                    logger.err('Could not create symlink `{}` referencing `{}`'.format(dest_file, source_file))
                    raise

    # Load the old list of symlinked files for this package
    if os.path.exists(devel_manifest_file_path):
        with open(devel_manifest_file_path, 'r') as devel_manifest:
            manifest_reader = csv.reader(devel_manifest, delimiter=' ', quotechar='"')
            # Skip the package source directory
            devel_manifest.readline()
            # Read the previously-generated products
            for source_file, dest_file in manifest_reader:
                # print('Checking (%s, %s)' % (source_file, dest_file))
                if (source_file, dest_file) not in products:
                    # Clean the file or decrement the collision count
                    logger.out('Cleaning: (%s, %s)' % (source_file, dest_file))
                    files_to_clean.append(dest_file)

    # Remove all listed symlinks and empty directories which have been removed
    # after this build, and update the collision file
    try:
        clean_linked_files(logger, event_queue, metadata_path, files_that_collide, files_to_clean, dry_run=False)
    except:  # noqa: E722
        # Silencing E722 here since we immediately re-raise the exception.
        logger.err('Could not clean linked files.')
        raise

    # Save the list of symlinked files
    with open(devel_manifest_file_path, 'w') as devel_manifest:
        # Write the path to the package source directory
        devel_manifest.write('%s\n' % package_path)
        # Write all the products
        manifest_writer = csv.writer(devel_manifest, delimiter=' ', quotechar='"')
        for source_file, dest_file in products:
            manifest_writer.writerow([source_file, dest_file])

    return 0


def create_catkin_build_job(
        context,
        package,
        package_path,
        dependencies,
        force_cmake,
        pre_clean,
        skip_install,
        prebuild=False):
    """Job class for building catkin packages"""

    # Package source space path
    pkg_dir = os.path.join(context.source_space_abs, package_path)

    # Package build space path
    build_space = context.package_build_space(package)
    # Package devel space path
    devel_space = context.package_devel_space(package)
    # Package install space path
    install_space = context.package_install_space(package)
    # Package metadata path
    metadata_path = context.package_metadata_path(package)
    # Environment dictionary for the job, which will be built
    # up by the executions in the loadenv stage.
    job_env = dict(os.environ)

    # Create job stages
    stages = []

    # Load environment for job.
    stages.append(FunctionStage(
        'loadenv',
        loadenv,
        locked_resource=None if context.isolate_install else 'installspace',
        job_env=job_env,
        package=package,
        context=context
    ))

    # Create package build space
    stages.append(FunctionStage(
        'mkdir',
        makedirs,
        path=build_space
    ))

    # Create package metadata dir
    stages.append(FunctionStage(
        'mkdir',
        makedirs,
        path=metadata_path
    ))

    # Copy source manifest
    stages.append(FunctionStage(
        'cache-manifest',
        copyfiles,
        source_paths=[os.path.join(context.source_space_abs, package_path, 'package.xml')],
        dest_path=os.path.join(metadata_path, 'package.xml')
    ))

    # Only run CMake if the Makefile doesn't exist or if --force-cmake is given
    # TODO: This would need to be different with `cmake --build`
    makefile_path = os.path.join(build_space, 'Makefile')

    if not os.path.isfile(makefile_path) or force_cmake:

        require_command('cmake', CMAKE_EXEC)

        # CMake command
        stages.append(CommandStage(
            'cmake',
            [
                CMAKE_EXEC,
                pkg_dir,
                '--no-warn-unused-cli',
                '-DCATKIN_DEVEL_PREFIX=' + devel_space,
                '-DCMAKE_INSTALL_PREFIX=' + install_space
            ] + context.cmake_args,
            cwd=build_space,
            logger_factory=CMakeIOBufferProtocol.factory_factory(pkg_dir),
            occupy_job=True
        ))
    else:
        # Check buildsystem command
        stages.append(CommandStage(
            'check',
            [MAKE_EXEC, 'cmake_check_build_system'],
            cwd=build_space,
            logger_factory=CMakeIOBufferProtocol.factory_factory(pkg_dir),
            occupy_job=True
        ))

    # Filter make arguments
    make_args = handle_make_arguments(
        context.make_args +
        context.catkin_make_args)

    # Pre-clean command
    if pre_clean:
        # TODO: Remove target args from `make_args`
        stages.append(CommandStage(
            'preclean',
            [MAKE_EXEC, 'clean'] + make_args,
            cwd=build_space,
        ))

    require_command('make', MAKE_EXEC)

    # Make command
    stages.append(CommandStage(
        'make',
        [MAKE_EXEC] + make_args,
        cwd=build_space,
        logger_factory=CMakeMakeIOBufferProtocol.factory
    ))

    # Symlink command if using a linked develspace
    if context.link_devel:
        stages.append(FunctionStage(
            'symlink',
            link_devel_products,
            locked_resource='symlink-collisions-file',
            package=package,
            package_path=package_path,
            devel_manifest_path=context.package_metadata_path(package),
            source_devel_path=context.package_devel_space(package),
            dest_devel_path=context.devel_space_abs,
            metadata_path=context.metadata_path(),
            prebuild=prebuild
        ))

    # Make install command, if installing
    if context.install and not skip_install:
        stages.append(CommandStage(
            'install',
            [MAKE_EXEC, 'install'],
            cwd=build_space,
            logger_factory=CMakeMakeIOBufferProtocol.factory,
            locked_resource=None if context.isolate_install else 'installspace'
        ))
        # Copy install manifest
        stages.append(FunctionStage(
            'register',
            copy_install_manifest,
            src_install_manifest_path=build_space,
            dst_install_manifest_path=context.package_metadata_path(package)
        ))

    return Job(
        jid=package.name,
        deps=dependencies,
        env=job_env,
        stages=stages)


def create_catkin_clean_job(
        context,
        package,
        package_path,
        dependencies,
        dry_run,
        clean_build,
        clean_devel,
        clean_install):
    """Generate a Job that cleans a catkin package"""

    stages = []

    # Package build space path
    build_space = context.package_build_space(package)
    # Package metadata path
    metadata_path = context.package_metadata_path(package)
    # Environment dictionary for the job, empty for a clean job
    job_env = {}

    # Remove installed files
    if clean_install:
        installed_files = get_installed_files(context.package_metadata_path(package))
        install_dir = context.package_install_space(package)
        if context.merge_install:
            # Don't clean shared files in a merged install space layout.
            installed_files = [
                path for path in installed_files
                if os.path.dirname(path) != install_dir
            ]
        # If a Python package with the package name is installed, clean it too.
        python_dir = os.path.join(install_dir, get_python_install_dir(context), package.name)
        if os.path.exists(python_dir):
            installed_files.append(python_dir)
        stages.append(FunctionStage(
            'cleaninstall',
            rmfiles,
            paths=sorted(installed_files),
            remove_empty=True,
            empty_root=context.install_space_abs,
            dry_run=dry_run))

    # Remove products in develspace
    if clean_devel:
        if context.merge_devel:
            # Remove build targets from devel space
            stages.append(CommandStage(
                'clean',
                [MAKE_EXEC, 'clean'],
                cwd=build_space,
            ))
        elif context.link_devel:
            # Remove symlinked products
            stages.append(FunctionStage(
                'unlink',
                unlink_devel_products,
                locked_resource='symlink-collisions-file',
                devel_space_abs=context.devel_space_abs,
                private_devel_path=context.package_private_devel_path(package),
                metadata_path=context.metadata_path(),
                package_metadata_path=context.package_metadata_path(package),
                dry_run=dry_run
            ))

            # Remove devel space
            stages.append(FunctionStage(
                'rmdevel',
                rmfiles,
                paths=[context.package_private_devel_path(package)],
                dry_run=dry_run))
        elif context.isolate_devel:
            # Remove devel space
            stages.append(FunctionStage(
                'rmdevel',
                rmfiles,
                paths=[context.package_devel_space(package)],
                dry_run=dry_run))

    # Remove build space
    if clean_build:
        stages.append(FunctionStage(
            'rmbuild',
            rmfiles,
            paths=[build_space],
            dry_run=dry_run))

    # Remove cached metadata
    if clean_build and clean_devel and clean_install:
        stages.append(FunctionStage(
            'rmmetadata',
            rmfiles,
            paths=[metadata_path],
            dry_run=dry_run))

    return Job(
        jid=package.name,
        deps=dependencies,
        env=job_env,
        stages=stages)


def create_catkin_test_job(
    context,
    package,
    package_path,
    test_target,
    verbose,
):
    """Generate a job that tests a package"""

    # Package source space path
    pkg_dir = os.path.join(context.source_space_abs, package_path)
    # Package build space path
    build_space = context.package_build_space(package)
    # Environment dictionary for the job, which will be built
    # up by the executions in the loadenv stage.
    job_env = dict(os.environ)

    # Create job stages
    stages = []

    # Load environment for job
    stages.append(FunctionStage(
        'loadenv',
        loadenv,
        locked_resource=None,
        job_env=job_env,
        package=package,
        context=context,
        verbose=False,
    ))

    # Check buildsystem command
    # The stdout is suppressed here instead of globally because for the actual tests,
    # stdout contains important information, but for cmake it is only relevant when verbose
    stages.append(CommandStage(
        'check',
        [MAKE_EXEC, 'cmake_check_build_system'],
        cwd=build_space,
        logger_factory=CMakeIOBufferProtocol.factory_factory(pkg_dir, suppress_stdout=not verbose),
        occupy_job=True
    ))

    # Check if the test target exists
    # make -q target_name returns 2 if the target does not exist, in that case we want to terminate this test job
    # the other cases (0=target is up-to-date, 1=target exists but is not up-to-date) can be ignored
    stages.append(CommandStage(
        'findtest',
        [MAKE_EXEC, '-q', test_target],
        cwd=build_space,
        early_termination_retcode=2,
        success_retcodes=(0, 1, 2),
    ))

    # Make command
    stages.append(CommandStage(
        'make',
        [MAKE_EXEC, test_target] + context.make_args,
        cwd=build_space,
        logger_factory=CMakeMakeRunTestsIOBufferProtocol.factory_factory(verbose),
    ))

    # catkin_test_results
    result_cmd = ['catkin_test_results']
    if verbose:
        result_cmd.append('--verbose')
    stages.append(CommandStage(
        'results',
        result_cmd,
        cwd=build_space,
        logger_factory=CatkinTestResultsIOBufferProtocol.factory,
    ))

    return Job(
        jid=package.name,
        deps=[],
        env=job_env,
        stages=stages,
    )


description = dict(
    build_type='catkin',
    description="Builds a catkin package.",
    create_build_job=create_catkin_build_job,
    create_clean_job=create_catkin_clean_job,
    create_test_job=create_catkin_test_job,
)


DEVEL_MANIFEST_FILENAME = 'devel_manifest.txt'

# List of files which shouldn't be copied
DEVEL_LINK_PREBUILD_SKIPLIST = [
    '.catkin',
    '.rosinstall',
]
DEVEL_LINK_SKIPLIST = DEVEL_LINK_PREBUILD_SKIPLIST + [
    os.path.join('etc', 'catkin', 'profile.d', '05.catkin_make.bash'),
    os.path.join('etc', 'catkin', 'profile.d', '05.catkin_make_isolated.bash'),
    os.path.join('etc', 'catkin', 'profile.d', '05.catkin-test-results.sh'),
    'env.sh',
    'setup.bash',
    'setup.fish',
    'setup.zsh',
    'setup.sh',
    'local_setup.bash',
    'local_setup.fish',
    'local_setup.zsh',
    'local_setup.sh',
    '_setup_util.py',
]
DEVEL_LINK_SKIP_DIRECTORIES = [
    '__pycache__',
]
# Deprecated names
DEVEL_LINK_PREBUILD_BLACKLIST = DEVEL_LINK_PREBUILD_SKIPLIST
DEVEL_LINK_BLACKLIST = DEVEL_LINK_SKIPLIST

# CMakeLists.txt for prebuild package
SETUP_PREBUILD_CMAKELISTS_TEMPLATE = """\
cmake_minimum_required(VERSION 3.10)
project(catkin_tools_prebuild)

find_package(catkin QUIET)

if(catkin_FOUND)
  catkin_package()
else()
  # Generate an error here which is more helpful than the normal one generated by CMake.
  # TODO: It's possible that we could just do this silently, instead.

  message(FATAL_ERROR
"The catkin CMake module was not found, but it is required to build a linked workspace.\
 To resolve this, please do one of the following, and try building again.

 1. Source the setup.sh file from an existing catkin workspace:
    source SETUP_FILE

 2. Extend another catkin workspace's result (install or devel) space:
    catkin config --extend RESULT_SPACE

 3. Set `catkin_DIR` to the directory containing `catkin-config.cmake`:
    catkin config --cmake-args -Dcatkin_DIR=CATKIN_CMAKE_CONFIG_PATH

 4. Add the catkin source package to your workspace's source space:
    cd SOURCE_SPACE && git clone https://github.com/ros/catkin.git")
endif()
"""

# package.xml file for prebuild package
SETUP_PREBUILD_PACKAGE_XML_TEMPLATE = """\
<package>
  <name>catkin_tools_prebuild</name>
  <description>
    This package is used to generate catkin setup files.
  </description>
  <version>0.0.0</version>
  <license>BSD</license>
  <maintainer email="jbo@jhu.edu">jbohren</maintainer>
  <buildtool_depend>catkin</buildtool_depend>
</package>
"""

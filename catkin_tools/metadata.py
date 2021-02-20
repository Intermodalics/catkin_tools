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

from __future__ import print_function

import os
import pkg_resources
import shutil
import sys
import yaml

from .common import mkdir_p

METADATA_DIR_NAME = '.catkin_tools'

METADATA_README_TEXT = """\
# Catkin Tools Metadata

This directory was generated by catkin_tools and it contains persistent
configuration information used by the `catkin` command and its sub-commands.

Each subdirectory of the `profiles` directory contains a set of persistent
configuration options for separate profiles. The default profile is called
`default`. If another profile is desired, it can be described in the
`profiles.yaml` file in this directory.

Please see the catkin_tools documentation before editing any files in this
directory. Most actions can be performed with the `catkin` command-line
program.
"""

PROFILES_YML_FILE_NAME = 'profiles.yaml'

DEFAULT_PROFILE_NAME = 'default'


def get_metadata_root_path(workspace_path):
    """Construct the path to a root metadata directory.

    :param workspace_path: The exact path to the root of a catkin_tools workspace
    :type workspace_path: str

    :returns: The path to the metadata root directory or None if workspace_path isn't a string
    :rtype: str or None
    """

    # TODO: Should calling this without a string just be a fatal error?
    if workspace_path is None:
        return None

    return os.path.join(workspace_path, METADATA_DIR_NAME)


def get_profiles_path(workspace_path):
    """Construct the path to a root metadata directory.

    :param workspace_path: The exact path to the root of a catkin_tools workspace
    :type workspace_path: str

    :returns: The path to the profile metadata directory or None if workspace_path isn't a string
    :rtype: str or None
    """

    if workspace_path is None:
        return None

    return os.path.join(workspace_path, METADATA_DIR_NAME, 'profiles')


def get_paths(workspace_path, profile_name, verb=None):
    """Construct the path to a metadata directory and verb-specific metadata file.

    Note: these paths are not guaranteed to exist. This function simply serves
    to standardize where these files should be located.

    :param workspace_path: The path to the root of a catkin workspace
    :type workspace_path: str
    :param profile_name: The catkin_tools metadata profile name
    :type profile_name: str
    :param verb: (optional) The catkin_tools verb with which this information is associated.

    :returns: A tuple of the metadata directory and the verb-specific file path, if given
    """

    # Get the root of the metadata directory
    profiles_path = get_profiles_path(workspace_path)

    # Get the active profile directory
    metadata_path = os.path.join(profiles_path, profile_name) if profile_name else None

    # Get the metadata for this verb
    metadata_file_path = os.path.join(metadata_path, '%s.yaml' % verb) if profile_name and verb else None

    return (metadata_path, metadata_file_path)

def find_enclosing_workspaces(search_start_path):
    """Find a catkin workspaces based on the existence of a catkin_tools
    metadata directory starting in the path given by search_path and traversing
    each parent directory and returns a list of workspaces.

    :search_start_path: Directory which either is a catkin workspace or is
    contained in a catkin workspace

    :returns: List of path to the workspaces if found, an empty list otherwise.
    """
    workspaces = []
    while search_start_path:
        # Check if marker file exists
        candidate_path = os.path.join(search_start_path, METADATA_DIR_NAME)
        if os.path.exists(candidate_path) and os.path.isdir(candidate_path):
            workspaces.append(search_start_path)

        # Update search path or end
        (search_start_path, child_path) = os.path.split(search_start_path)
        if len(child_path) == 0:
            break

    return workspaces

def find_enclosing_workspace(search_start_path):
    """Find a catkin workspace based on the existence of a catkin_tools
    metadata directory starting in the path given by search_path and traversing
    each parent directory until either finding such a directory or getting to
    the root of the filesystem.

    If more than one candidate exists, returns the topmost workspace (closest
    to the root of the filesystem.

    :search_start_path: Directory which either is a catkin workspace or is
    contained in a catkin workspace

    :returns: Path to the workspace if found, `None` if not found.
    """
    workspaces = find_enclosing_workspaces(search_start_path)
    if not workspaces:
        return None
    return workspaces[-1]

def migrate_metadata(workspace_path):
    """Migrate metadata if it's out of date."""
    metadata_root_path = get_metadata_root_path(workspace_path)

    # Nothing there to migrate
    if not metadata_root_path or not os.path.exists(metadata_root_path):
        return

    # Check metadata version
    last_version = None
    current_version = pkg_resources.require("catkin_tools")[0].version
    version_file_path = os.path.join(metadata_root_path, 'VERSION')

    # Read the VERSION file
    if os.path.exists(version_file_path):
        with open(version_file_path, 'r') as metadata_version:
            last_version = metadata_version.read() or None
    if last_version != current_version:
        # Write the VERSION file
        with open(version_file_path, 'w') as metadata_version:
            metadata_version.write(current_version)
        migrate_metadata_version(workspace_path, last_version)


def migrate_metadata_version(workspace_path, old_version):
    """Migrate between metadata versions.
    """

    # Versions were added in 0.4.0, and the previously released version was 0.3.1
    if old_version is None:
        old_version = '0.3.1'

    old_version = tuple((int(i) for i in old_version.split('.')))
    metadata_root_path = get_metadata_root_path(workspace_path)
    new_profiles_path = os.path.join(metadata_root_path, 'profiles')

    # Restructure profiles directory
    if old_version < (0, 4, 0):

        for profile_name in os.listdir(metadata_root_path):

            if profile_name == 'profiles':
                continue

            profile_path = os.path.join(metadata_root_path, profile_name)
            if not os.path.isdir(profile_path):
                continue

            new_path = os.path.join(new_profiles_path, profile_name)
            shutil.move(profile_path, new_path)

    # Update metadata
    for profile_name in get_profile_names(workspace_path):
        for verb in ['config', 'build']:

            # Get the current metadata
            metadata = get_metadata(workspace_path, profile_name, verb)

            # Update devel layout for 0.3.1 -> 0.4.0
            if old_version < (0, 4, 0):
                isolate_devel = metadata.get('isolate_devel')
                if isolate_devel is not None:
                    del metadata['isolate_devel']
                devel_layout = ('isolated' if isolate_devel else 'merged')
                metadata['devel_layout'] = devel_layout

            # Save the new metadata
            update_metadata(workspace_path, profile_name, verb, metadata, no_init=True, merge=False)


def init_metadata_root(workspace_path, reset=False):
    """Create or reset a catkin_tools metadata directory with no content in a given path.

    :param workspace_path: The exact path to the root of a catkin workspace
    :type workspace_path: str
    :param reset: If true, clear the metadata directory of all information
    :type reset: bool
    """

    # Make sure the directory
    if not os.path.exists(workspace_path):
        raise IOError(
            "Can't initialize Catkin workspace in path %s because it does "
            "not exist." % (workspace_path))

    # Construct the full path to the metadata directory
    metadata_root_path = get_metadata_root_path(workspace_path)

    # Check if a metadata directory already exists
    if os.path.exists(metadata_root_path):
        # Reset the directory if requested
        if reset:
            print("Deleting existing metadata from catkin_tools metadata directory: %s" % (metadata_root_path))
            shutil.rmtree(metadata_root_path)
            os.mkdir(metadata_root_path)
    else:
        # Create a new .catkin_tools directory
        os.mkdir(metadata_root_path)

    # Write the README file describing the directory
    with open(os.path.join(metadata_root_path, 'README'), 'w') as metadata_readme:
        metadata_readme.write(METADATA_README_TEXT)

    # Migrate the metadata, if necessary
    migrate_metadata(workspace_path)

    # Add a catkin ignore file so we can store package.xml files for cleaned packages
    if not os.path.exists(os.path.join(metadata_root_path, 'CATKIN_IGNORE')):
        open(os.path.join(metadata_root_path, 'CATKIN_IGNORE'), 'a').close()


def init_profile(workspace_path, profile_name, reset=False):
    """Initialize a profile directory in a given workspace.

    :param workspace_path: The exact path to the root of a catkin_tools workspace
    :type workspace_path: str
    :param profile_name: The catkin_tools metadata profile name to initialize
    :type profile_name: str
    """

    init_metadata_root(workspace_path)

    (profile_path, _) = get_paths(workspace_path, profile_name)

    # Check if a profile directory already exists
    if os.path.exists(profile_path):
        # Reset the directory if requested
        if reset:
            print("Deleting existing profile from catkin_tools profile directory: %s" % (profile_path))
            shutil.rmtree(profile_path)
            os.mkdir(profile_path)
    else:
        # Create a new .catkin_tools directory
        mkdir_p(profile_path)


def get_profile_names(workspace_path):
    """Get a list of profile names available to a given workspace.

    :param workspace_path: The exact path to the root of a catkin_tools workspace
    :type workspace_path: str

    :returns: A list of the available profile names in the given workspace
    :rtype: list
    """

    migrate_metadata(workspace_path)

    profiles_path = get_profiles_path(workspace_path)

    if os.path.exists(profiles_path):
        directories = next(os.walk(profiles_path))[1]

        return directories

    return []


def remove_profile(workspace_path, profile_name):
    """Remove a profile by name.

    :param workspace_path: The exact path to the root of a catkin_tools workspace
    :type workspace_path: str
    :param profile_name: The catkin_tools metadata profile name to delete
    :type profile_name: str
    """

    migrate_metadata(workspace_path)

    (profile_path, _) = get_paths(workspace_path, profile_name)

    if os.path.exists(profile_path):
        shutil.rmtree(profile_path)


def set_active_profile(workspace_path, profile_name):
    """Set a profile in a given workspace to be active.

    :param workspace_path: The exact path to the root of a catkin_tools workspace
    :type workspace_path: str
    :param profile_name: The catkin_tools metadata profile name to activate
    :type profile_name: str
    """

    profiles_data = get_profiles_data(workspace_path)
    profiles_data['active'] = profile_name

    profiles_path = get_profiles_path(workspace_path)
    profiles_yaml_file_path = os.path.join(profiles_path, PROFILES_YML_FILE_NAME)
    with open(profiles_yaml_file_path, 'w') as profiles_file:
        yaml.dump(profiles_data, profiles_file, default_flow_style=False)


def get_active_profile(workspace_path):
    """Get the active profile name from a workspace path.

    :param workspace_path: The exact path to the root of a catkin_tools workspace
    :type workspace_path: str

    :returns: The active profile name
    :rtype: str
    """

    profiles_data = get_profiles_data(workspace_path)
    if 'active' in profiles_data:
        return profiles_data['active']

    return DEFAULT_PROFILE_NAME


def get_profiles_data(workspace_path):
    """Get the contents of the profiles file.

    This file contains information such as the currently active profile.

    :param workspace_path: The exact path to the root of a catkin_tools workspace
    :type workspace_path: str

    :returns: The contents of the root profiles file if it exists
    :rtype: dict
    """

    migrate_metadata(workspace_path)

    if workspace_path is not None:
        profiles_path = get_profiles_path(workspace_path)
        profiles_yaml_file_path = os.path.join(profiles_path, PROFILES_YML_FILE_NAME)
        if os.path.exists(profiles_yaml_file_path):
            with open(profiles_yaml_file_path, 'r') as profiles_file:
                return yaml.safe_load(profiles_file)

    return {}


def get_metadata(workspace_path, profile, verb):
    """Get a python structure representing the metadata for a given verb.

    :param workspace_path: The exact path to the root of a catkin workspace
    :type workspace_path: str
    :param profile: The catkin_tools metadata profile name
    :type profile: str
    :param verb: The catkin_tools verb with which this information is associated
    :type verb: str

    :returns: A python structure representing the YAML file contents (empty
    dict if the file does not exist)
    :rtype: dict
    """

    migrate_metadata(workspace_path)

    (metadata_path, metadata_file_path) = get_paths(workspace_path, profile, verb)

    if not os.path.exists(metadata_file_path) or os.path.getsize(metadata_file_path) == 0:
        return dict()

    with open(metadata_file_path, 'r') as metadata_file:
        return yaml.safe_load(metadata_file)


def update_metadata(workspace_path, profile, verb, new_data={}, no_init=False, merge=True):
    """Update the catkin_tools verb metadata for a given profile.

    :param workspace_path: The path to the root of a catkin workspace
    :type workspace_path: str
    :param profile: The catkin_tools metadata profile name
    :type profile: str
    :param verb: The catkin_tools verb with which this information is associated
    :type verb: str
    :param new_data: A python dictionary or array to write to the metadata file
    :type new_data: dict
    """

    migrate_metadata(workspace_path)

    (metadata_path, metadata_file_path) = get_paths(workspace_path, profile, verb)

    # Make sure the metadata directory exists
    if not no_init:
        init_metadata_root(workspace_path)
        init_profile(workspace_path, profile)

    # Get the curent metadata for this verb
    if merge:
        data = get_metadata(workspace_path, profile, verb)
    else:
        data = dict()

    # Update the metadata for this verb
    data.update(new_data)
    try:
        with open(metadata_file_path, 'w') as metadata_file:
            yaml.dump(data, metadata_file, default_flow_style=False)
    except PermissionError:
        print("Could not write to metadata file '%s'!" % (metadata_file_path))

    return data


def get_active_metadata(workspace_path, verb):
    """Get a python structure representing the metadata for a given verb.
    :param workspace_path: The exact path to the root of a catkin workspace
    :type workspace_path: str
    :param verb: The catkin_tools verb with which this information is associated
    :type verb: str

    :returns: A python structure representing the YAML file contents (empty
    dict if the file does not exist)
    :rtype: dict
    """

    active_profile = get_active_profile(workspace_path)
    get_metadata(workspace_path, active_profile, verb)


def update_active_metadata(workspace_path, verb, new_data={}):
    """Update the catkin_tools verb metadata for the active profile.

    :param workspace_path: The path to the root of a catkin workspace
    :type workspace_path: str
    :param verb: The catkin_tools verb with which this information is associated
    :type verb: str
    :param new_data: A python dictionary or array to write to the metadata file
    :type new_data: dict
    """

    active_profile = get_active_profile(workspace_path)
    update_active_metadata(workspace_path, active_profile, verb, new_data)

# Copyright 2013 Google Inc. All Rights Reserved.

"""Used to collect anonymous SDK metrics."""

import atexit
import hashlib
import os
import pickle
import socket
import subprocess
import tempfile
import time
import urllib
import uuid

from googlecloudsdk.core import config
from googlecloudsdk.core import log
from googlecloudsdk.core import properties
from googlecloudsdk.core.util import console_io
from googlecloudsdk.core.util import execution_utils
from googlecloudsdk.core.util import files
from googlecloudsdk.core.util import platforms



_GA_ENDPOINT = 'https://ssl.google-analytics.com/collect'
_GA_TID = 'UA-36037335-2'
_GA_INSTALLS_CATEGORY = 'Installs'
_GA_COMMANDS_CATEGORY = 'Commands'
_GA_HELP_CATEGORY = 'Help'
_GA_ERROR_CATEGORY = 'Error'
_GA_EXECUTIONS_CATEGORY = 'Executions'

_CSI_ENDPOINT = 'https://csi.gstatic.com/csi'
_CSI_ID = 'cloud_sdk'
_CSI_LOAD_EVENT = 'load'
_CSI_RUN_EVENT = 'run'
_CSI_TOTAL_EVENT = 'total'


class _GAEvent(object):

  def __init__(self, category, action, label, value):
    self.category = category
    self.action = action
    self.label = label
    self.value = value


def _GetTimeMillis(time_secs):
  return int(round(time_secs * 1000))


class _TimedEvent(object):

  def __init__(self, name):
    self.name = name
    self.time_millis = _GetTimeMillis(time.time())


class _CommandTimer(object):
  """A class for timing the execution of a command."""

  def __init__(self, start_time):
    self.__start = _GetTimeMillis(start_time)
    self.__events = []
    self.__action = 'unknown'

  def SetAction(self, action):
    self.__action = action.replace('.', ',').replace('-', '_')

  def Event(self, name):
    self.__events.append(_TimedEvent(name))

  def GetCSIParams(self):
    params = [('action', self.__action)]

    response_times = [
        '{0}.{1}'.format(event.name, event.time_millis - self.__start)
        for event in self.__events]
    params.append(('rt', ','.join(response_times)))

    return params


class _MetricsCollector(object):
  """A singleton class to handle metrics reporting."""

  _disabled_cache = None
  _instance = None

  @staticmethod
  def GetCollectorIfExists():
    return _MetricsCollector._instance

  @staticmethod
  def GetCollector():
    """Returns the singleton _MetricsCollector instance or None if disabled."""
    if _MetricsCollector._IsDisabled():
      return None

    if not _MetricsCollector._instance:
      _MetricsCollector._instance = _MetricsCollector()
    return _MetricsCollector._instance

  @staticmethod
  def _IsDisabled():
    """Returns whether metrics collection should be disabled."""
    if _MetricsCollector._disabled_cache is None:
      # Don't collect metrics for completions.
      if '_ARGCOMPLETE' in os.environ:
        _MetricsCollector._disabled_cache = True
      else:
        # Don't collect metrics if the user has opted out.
        disabled = properties.VALUES.core.disable_usage_reporting.GetBool()
        if disabled is None:
          # There is no preference set, fall back to the installation default.
          disabled = config.INSTALLATION_CONFIG.disable_usage_reporting
        _MetricsCollector._disabled_cache = disabled
    return _MetricsCollector._disabled_cache

  def __init__(self):
    """Initialize a new MetricsCollector.

    This should only be invoked through the static GetCollector() function.
    """
    current_platform = platforms.Platform.Current()
    self._user_agent = 'CloudSDK/{version} {fragment}'.format(
        version=config.CLOUD_SDK_VERSION,
        fragment=current_platform.UserAgentFragment())
    self._async_popen_args = current_platform.AsycPopenArgs()
    self._project_ids = {}

    hostname = socket.getfqdn()
    install_type = 'Google' if hostname.endswith('.google.com') else 'External'
    self._ga_params = [
        ('v', '1'),
        ('tid', _GA_TID),
        ('cid', _MetricsCollector._GetCID()),
        ('t', 'event'),
        ('cd1', config.INSTALLATION_CONFIG.release_channel),
        ('cd2', install_type),
        ('cd3', properties.VALUES.metrics.environment.Get()),
        ('cd4', console_io.IsInteractive(error=True, heuristic=True))]

    self._csi_params = [('s', _CSI_ID),
                        ('v', '2'),
                        ('rls', config.CLOUD_SDK_VERSION)]

    self.StartTimer(time.time())
    self._metrics = []

    log.debug('Metrics collector initialized...')

  @staticmethod
  def _GetCID():
    """Gets the client id from the config file, or generates a new one.

    Returns:
      str, The hex string of the client id.
    """
    uuid_path = config.Paths().analytics_cid_path
    cid = None
    if os.path.exists(uuid_path):
      with open(uuid_path) as f:
        cid = f.read()
      if cid:
        return cid

    files.MakeDir(os.path.dirname(uuid_path))
    with open(uuid_path, 'w') as f:
      cid = uuid.uuid4().hex
      f.write(cid)  # A random UUID

    return cid

  def _GetProjectIDHash(self):
    """Gets the hash of the current project id.

    Returns:
      str, The hex digest of the current project id or None if the
      project is not set.
    """
    project_id = properties.VALUES.core.project.Get(validate=False)
    if not project_id:
      return None
    hashed_id = self._project_ids.get(project_id)
    if not hashed_id:
      checksum = hashlib.sha1()
      checksum.update(project_id)
      hashed_id = checksum.hexdigest()
      self._project_ids[project_id] = hashed_id
    return hashed_id

  def StartTimer(self, start_time):
    self._timer = _CommandTimer(start_time)

  def RecordTimedEvent(self, name):
    self._timer.Event(name)

  def SetTimerAction(self, action):
    self._timer.SetAction(action)

  def CollectCSIMetric(self):
    """Adds metric with latencies for the given command to the metrics queue."""
    params = self._timer.GetCSIParams()
    params.extend(self._csi_params)
    data = urllib.urlencode(params)

    self._metrics.append(
        ('{0}?{1}'.format(_CSI_ENDPOINT, data), 'GET', None, self._user_agent))

  def CollectGAMetric(self, event):
    """Adds the given GA event to the metrics queue.

    Args:
      event: _Event, The event to process.
    """
    params = [
        ('ec', event.category),
        ('ea', event.action),
        ('el', event.label),
        ('ev', event.value),
    ]
    project_id_hash = self._GetProjectIDHash()
    if project_id_hash:
      params.append(('cd11', project_id_hash))
    params.extend(self._ga_params)
    data = urllib.urlencode(params)

    self._metrics.append((_GA_ENDPOINT, 'POST', data, self._user_agent))

  def ReportMetrics(self):
    """Reports the collected metrics using a separate async process."""
    if not self._metrics:
      return

    temp_metrics_file = tempfile.NamedTemporaryFile(delete=False)
    with temp_metrics_file:
      pickle.dump(self._metrics, temp_metrics_file)
      self._metrics = []

    reporting_script_path = os.path.join(
        config.GoogleCloudSDKPackageRoot(), 'core', 'metrics_reporter.py')
    execution_args = execution_utils.ArgsForPythonTool(
        reporting_script_path, temp_metrics_file.name)

    exec_env = os.environ.copy()
    python_path_var = 'PYTHONPATH'
    python_path = exec_env.get(python_path_var)
    if python_path:
      python_path += os.pathsep + config.LibraryRoot()
    else:
      python_path = config.LibraryRoot()
    exec_env[python_path_var] = python_path

    subprocess.Popen(execution_args, env=exec_env, **self._async_popen_args)
    log.debug('Metrics reporting process started...')


def _CollectGAMetricAndSetTimerAction(category, action, label, value=0):
  """Common code for processing a GA event."""
  collector = _MetricsCollector.GetCollector()
  if collector:
    collector.CollectGAMetric(
        _GAEvent(category=category, action=action, label=label, value=value))

    # Dont include version. We already send it as the rls CSI parameter.
    if category is _GA_COMMANDS_CATEGORY or category is _GA_EXECUTIONS_CATEGORY:
      collector.SetTimerAction('{0}.{1}'.format(category, action))
    elif category is _GA_ERROR_CATEGORY or category is _GA_HELP_CATEGORY:
      collector.SetTimerAction('{0}.{1}.{2}'.format(category, action, label))
    # Ignoring installs for now since there could be multiple per cmd execution.


def CaptureAndLogException(func):
  """Function decorator to capture and log any exceptions."""
  def Wrapper(*args, **kwds):
    try:
      return func(*args, **kwds)
    # pylint:disable=bare-except
    except:
      log.debug('Exception captured in %s', func.func_name, exc_info=True)
  return Wrapper


@CaptureAndLogException
@atexit.register
def Shutdown():
  """Reports the metrics that were collected."""
  collector = _MetricsCollector.GetCollectorIfExists()
  if collector:
    collector.RecordTimedEvent(_CSI_TOTAL_EVENT)
    collector.CollectCSIMetric()
    collector.ReportMetrics()


@CaptureAndLogException
def Installs(component_id, version_string):
  """Logs that an SDK component was installed.

  Args:
    component_id: str, The component id that was installed.
    version_string: str, The version of the component.
  """
  _CollectGAMetricAndSetTimerAction(
      _GA_INSTALLS_CATEGORY, component_id, version_string)


@CaptureAndLogException
def Commands(command_path, version_string):
  """Logs that a gcloud command was run.

  Args:
    command_path: str, The '.' separated name of the calliope command.
    version_string: str, The version of the command.
  """
  if not version_string:
    version_string = 'unknown'
  _CollectGAMetricAndSetTimerAction(
      _GA_COMMANDS_CATEGORY, command_path, version_string)


@CaptureAndLogException
def Help(command_path, mode):
  """Logs that help for a gcloud command was run.

  Args:
    command_path: str, The '.' separated name of the calliope command.
    mode: str, The way help was invoked (-h, --help, help).
  """
  _CollectGAMetricAndSetTimerAction(_GA_HELP_CATEGORY, command_path, mode)


@CaptureAndLogException
def Error(command_path, exc):
  """Logs that a top level Exception was caught for a gcloud command.

  Args:
    command_path: str, The '.' separated name of the calliope command.
    exc: Exception, The exception that was caught.
  """
  try:
    cls = exc.__class__
    name = '{0}.{1}'.format(cls.__module__, cls.__name__)
  # pylint:disable=bare-except, Never want to fail on metrics reporting.
  except:
    name = 'unknown'
  _CollectGAMetricAndSetTimerAction(_GA_ERROR_CATEGORY, command_path, name)


@CaptureAndLogException
def Executions(command_name, version_string):
  """Logs that a top level SDK script was run.

  Args:
    command_name: str, The script name.
    version_string: str, The version of the command.
  """
  if not version_string:
    version_string = 'unknown'
  _CollectGAMetricAndSetTimerAction(
      _GA_EXECUTIONS_CATEGORY, command_name, version_string)


@CaptureAndLogException
def Started(start_time):
  """Record the time when the command was started."""
  collector = _MetricsCollector.GetCollector()
  if collector:
    collector.StartTimer(start_time)


@CaptureAndLogException
def Loaded():
  """Record the time when command loading was completed."""
  collector = _MetricsCollector.GetCollector()
  if collector:
    collector.RecordTimedEvent(_CSI_LOAD_EVENT)


@CaptureAndLogException
def Ran():
  """Record the time when command running was completed."""
  collector = _MetricsCollector.GetCollector()
  if collector:
    collector.RecordTimedEvent(_CSI_RUN_EVENT)

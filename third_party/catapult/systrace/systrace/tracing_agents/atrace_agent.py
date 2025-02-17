# Copyright 2015 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import re
import py_utils
import subprocess
import sys
import threading
import zlib

from devil.android import device_utils
from py_trace_event import trace_time
from systrace import trace_result
from systrace import tracing_agents
from systrace import util


# Text that ADB sends, but does not need to be displayed to the user.
ADB_IGNORE_REGEXP = r'^capturing trace\.\.\. done|^capturing trace\.\.\.'
# The number of seconds to wait on output from ADB.
ADB_STDOUT_READ_TIMEOUT = 0.2
# The adb shell command to initiate a trace.
ATRACE_BASE_ARGS = ['atrace']
# If a custom list of categories is not specified, traces will include
# these categories (if available on the device).
DEFAULT_CATEGORIES = 'sched gfx view dalvik webview input disk am wm'.split()
# The command to list trace categories.
LIST_CATEGORIES_ARGS = ATRACE_BASE_ARGS + ['--list_categories']
# Minimum number of seconds between displaying status updates.
MIN_TIME_BETWEEN_STATUS_UPDATES = 0.2
# ADB sends this text to indicate the beginning of the trace data.
TRACE_START_REGEXP = r'TRACE\:'
# Plain-text trace data should always start with this string.
TRACE_TEXT_HEADER = '# tracer'
# The property name for switching on and off tracing during boot.
BOOTTRACE_PROP = 'persist.debug.atrace.boottrace'
# The file path for specifying categories to be traced during boot.
BOOTTRACE_CATEGORIES = '/data/misc/boottrace/categories'


def list_categories(options):
  """List the possible trace event categories.

  This function needs the tracing options since it needs to get the serial
  number of the device to send a command to.

  Args:
      options: Tracing options.
  """
  devutils = device_utils.DeviceUtils(options.device_serial_number)
  print '\n'.join(devutils.RunShellCommand(LIST_CATEGORIES_ARGS))
  if not devutils.HasRoot():
    print '\nNOTE: more categories may be available with adb root\n'


def get_available_categories(options):
  """Gets the list of atrace categories available for tracing.
  Args:
      options: Tracing options.
  """
  devutils = device_utils.DeviceUtils(options.device_serial_number)
  categories_output = devutils.RunShellCommand(LIST_CATEGORIES_ARGS)
  return [c.split('-')[0].strip() for c in categories_output]


def try_create_agent(options):
  """Create an Atrace agent.

  Args:
      options: Command line options.
      categories: Categories of trace events to capture.
  """
  if options.target != 'android':
    return False
  if options.from_file is not None:
    return False

  # Check device SDK version.
  device_sdk_version = util.get_device_sdk_version()
  if device_sdk_version <= 17:
    print ('Device SDK versions <= 17 not supported.\n'
           'Your device SDK version is %d.' % device_sdk_version)
    return False
  if device_sdk_version <= 22 and options.boot:
    print ('--boot option does not work on the device SDK '
           'version 22 or before.\nYour device SDK version '
           'is %d.' % device_sdk_version)
    return False

  return BootAgent() if options.boot else AtraceAgent()

def _construct_extra_atrace_args(options, categories):
  """Construct extra arguments (-a, -k, categories) for atrace command.

  Args:
      options: Tracing options.
      categories: Categories of trace events to capture.
  """
  extra_args = []

  if options.app_name is not None:
    extra_args.extend(['-a', options.app_name])

  if options.kfuncs is not None:
    extra_args.extend(['-k', options.kfuncs])

  extra_args.extend(categories)
  return extra_args

def _construct_atrace_args(options, categories):
  """Builds the command used to invoke a trace process.
  Returns:
    A tuple where the first element is an array of command arguments, and
    the second element is a boolean which will be true if the command will
    stream trace data.
  """
  atrace_args = ATRACE_BASE_ARGS[:]

  if options.compress_trace_data:
    atrace_args.extend(['-z'])

  if (options.trace_time is not None) and (options.trace_time > 0):
    atrace_args.extend(['-t', str(options.trace_time)])

  if (options.trace_buf_size is not None) and (options.trace_buf_size > 0):
    atrace_args.extend(['-b', str(options.trace_buf_size)])

  elif 'sched' in categories:
    # 'sched' is a high-volume tag, double the default buffer size
    # to accommodate that
    atrace_args.extend(['-b', '4096'])
  extra_args = _construct_extra_atrace_args(options, categories)

  atrace_args.extend(extra_args)
  return atrace_args

class AtraceAgent(tracing_agents.TracingAgent):

  def __init__(self):
    super(AtraceAgent, self).__init__()
    self._adb = None
    self._trace_data = None
    self._tracer_args = None
    self._collection_thread = None
    self._device_utils = None
    self._device_serial_number = None
    self._options = None
    self._categories = None

  @py_utils.Timeout(tracing_agents.START_STOP_TIMEOUT)
  def StartAgentTracing(self, options, categories, timeout=None):
    self._options = options
    self._categories = categories
    if not self._categories:
      self._categories = DEFAULT_CATEGORIES
    avail_cats = get_available_categories(options)
    unavailable = [x for x in self._categories if x not in avail_cats]
    self._categories = [x for x in self._categories if x in avail_cats]
    if unavailable:
      print 'These categories are unavailable: ' + ' '.join(unavailable)
    self._device_utils = device_utils.DeviceUtils(options.device_serial_number)
    self._device_serial_number = options.device_serial_number
    self._tracer_args = _construct_atrace_args(options, self._categories)
    self._device_utils.RunShellCommand(self._tracer_args + ['--async_start'])
    return True

  def _collect_and_preprocess(self):
    """Collects and preprocesses trace data.

    Stores results in self._trace_data.
    """
    trace_data = self._collect_trace_data()
    self._trace_data = self._preprocess_trace_data(trace_data)

  @py_utils.Timeout(tracing_agents.START_STOP_TIMEOUT)
  def StopAgentTracing(self, timeout=None):
    """Stops tracing and starts collecting results.

    To synchronously retrieve the results after calling this function,
    call GetResults().
    """
    self._collection_thread = threading.Thread(
        target=self._collect_and_preprocess)
    self._collection_thread.start()
    return True

  @py_utils.Timeout(tracing_agents.GET_RESULTS_TIMEOUT)
  def GetResults(self, timeout=None):
    """Waits for collection thread to finish and returns trace results."""
    self._collection_thread.join()
    self._collection_thread = None
    return trace_result.TraceResult('systemTraceEvents', self._trace_data)

  def SupportsExplicitClockSync(self):
    return True

  def RecordClockSyncMarker(self, sync_id, did_record_sync_marker_callback):
    """Records a clock sync marker.

    Args:
        sync_id: ID string for clock sync marker.
    """
    cmd = 'echo trace_event_clock_sync: name=%s >' \
        ' /sys/kernel/debug/tracing/trace_marker' % sync_id
    with self._device_utils.adb.PersistentShell(
        self._device_serial_number) as shell:
      t1 = trace_time.Now()
      shell.RunCommand(cmd, close=True)
      did_record_sync_marker_callback(t1, sync_id)

  def _dump_trace(self):
    """Dumps the atrace buffer and returns the dumped buffer."""
    dump_cmd = self._tracer_args + ['--async_dump']
    return self._device_utils.RunShellCommand(dump_cmd, raw_output=True)

  def _stop_trace(self):
    """Stops atrace.

    Tries to stop the atrace asynchronously. Note that on some devices,
    --async-stop does not work. Thus, this uses the fallback
    method of running a zero-length synchronous trace if that fails.
    """
    self._device_utils.RunShellCommand(self._tracer_args + ['--async_stop'])
    is_trace_enabled_cmd = ['cat', '/sys/kernel/debug/tracing/tracing_on']
    trace_on = int(self._device_utils.RunShellCommand(is_trace_enabled_cmd)[0])
    if trace_on:
      self._device_utils.RunShellCommand(self._tracer_args + ['-t 0'])

  def _collect_trace_data(self):
    """Reads the output from atrace and stops the trace."""
    result = self._dump_trace()
    data_start = re.search(TRACE_START_REGEXP, result)
    if data_start:
      data_start = data_start.end(0)
    else:
      raise IOError('Unable to get atrace data. Did you forget adb root?')
    output = re.sub(ADB_IGNORE_REGEXP, '', result[data_start:])
    self._stop_trace()
    return output

  def _preprocess_trace_data(self, trace_data):
    """Performs various processing on atrace data.

    Args:
      trace_data: The raw trace data.
    Returns:
      The processed trace data.
    """
    if trace_data:
      trace_data = strip_and_decompress_trace(trace_data)

    if not trace_data:
      print >> sys.stderr, ('No data was captured.  Output file was not '
                            'written.')
      sys.exit(1)

    if self._options.fix_threads:
      # Issue ps command to device and patch thread names
      ps_dump = do_preprocess_adb_cmd('ps -t',
                                      self._options.device_serial_number)
      if ps_dump is not None:
        thread_names = extract_thread_list(ps_dump)
        trace_data = fix_thread_names(trace_data, thread_names)

    if self._options.fix_tgids:
      # Issue printf command to device and patch tgids
      procfs_dump = do_preprocess_adb_cmd('printf "%s\n" ' +
                                          '/proc/[0-9]*/task/[0-9]*',
                                          self._options.device_serial_number)
      if procfs_dump is not None:
        pid2_tgid = extract_tgids(procfs_dump)
        trace_data = fix_missing_tgids(trace_data, pid2_tgid)

    if self._options.fix_circular:
      trace_data = fix_circular_traces(trace_data)

    return trace_data


class BootAgent(AtraceAgent):
  """AtraceAgent that specializes in tracing the boot sequence."""

  def __init__(self):
    super(BootAgent, self).__init__()

  @py_utils.Timeout(tracing_agents.START_STOP_TIMEOUT)
  def StartAgentTracing(self, options, categories, timeout=None):
    self._options = options
    try:
      setup_args = _construct_boot_setup_command(options, categories)
      subprocess.check_call(setup_args)
    except OSError as error:
      print >> sys.stderr, (
          'The command "%s" failed with the following error:' %
          ' '.join(setup_args))
      print >> sys.stderr, '    ', error
      sys.exit(1)

  def _dump_trace(self): #called by StopAgentTracing
    """Dumps the running trace asynchronously and returns the dumped trace."""
    dump_cmd = _construct_boot_trace_command(self._options)
    return self._device_utils.RunShellCommand(dump_cmd, raw_output=True)

  def _stop_trace(self): # called by _collect_trace_data via StopAgentTracing
    # pylint: disable=no-self-use
    # This is a member function for consistency with AtraceAgent
    pass # don't need to stop separately; already done in dump_trace

def _construct_boot_setup_command(options, categories):
  echo_args = ['echo'] + categories + ['>', BOOTTRACE_CATEGORIES]
  setprop_args = ['setprop', BOOTTRACE_PROP, '1']
  reboot_args = ['reboot']
  return util.construct_adb_shell_command(
      echo_args + ['&&'] + setprop_args + ['&&'] + reboot_args,
      options.device_serial_number)

def _construct_boot_trace_command(options):
  atrace_args = ['atrace', '--async_stop']
  setprop_args = ['setprop', BOOTTRACE_PROP, '0']
  rm_args = ['rm', BOOTTRACE_CATEGORIES]
  return util.construct_adb_shell_command(
        atrace_args + ['&&'] + setprop_args + ['&&'] + rm_args,
        options.device_serial_number)


def extract_thread_list(trace_text):
  """Removes the thread list from the given trace data.

  Args:
    trace_text: The text portion of the trace

  Returns:
    a map of thread ids to thread names
  """

  threads = {}
  # start at line 1 to skip the top of the ps dump:
  text = trace_text.splitlines()
  for line in text[1:]:
    cols = line.split(None, 8)
    if len(cols) == 9:
      tid = int(cols[1])
      name = cols[8]
      threads[tid] = name

  return threads


def extract_tgids(trace_text):
  """Removes the procfs dump from the given trace text

  Args:
    trace_text: The text portion of the trace

  Returns:
    a map of pids to their tgid.
  """
  tgid_2pid = {}
  text = trace_text.splitlines()
  for line in text:
    result = re.match('^/proc/([0-9]+)/task/([0-9]+)', line)
    if result:
      parent_pid, tgid = result.group(1, 2)
      tgid_2pid[tgid] = parent_pid

  return tgid_2pid


def strip_and_decompress_trace(trace_data):
  """Fixes new-lines and decompresses trace data.

  Args:
    trace_data: The trace data returned by atrace.
  Returns:
    The decompressed trace data.
  """
  # Collapse CRLFs that are added by adb shell.
  if trace_data.startswith('\r\n'):
    trace_data = trace_data.replace('\r\n', '\n')
  elif trace_data.startswith('\r\r\n'):
    # On windows, adb adds an extra '\r' character for each line.
    trace_data = trace_data.replace('\r\r\n', '\n')

  # Skip the initial newline.
  if trace_data[0] == '\n':
    trace_data = trace_data[1:]

  if not trace_data.startswith(TRACE_TEXT_HEADER):
    # No header found, so assume the data is compressed.
    trace_data = zlib.decompress(trace_data)

  # Enforce Unix line-endings.
  trace_data = trace_data.replace('\r', '')

  # Skip any initial newlines.
  while trace_data and trace_data[0] == '\n':
    trace_data = trace_data[1:]

  return trace_data


def fix_thread_names(trace_data, thread_names):
  """Replaces thread ids with their names.

  Args:
    trace_data: The atrace data.
    thread_names: A mapping of thread ids to thread names.
  Returns:
    The updated trace data.
  """

  def repl(m):
    tid = int(m.group(2))
    if tid > 0:
      name = thread_names.get(tid)
      if name is None:
        name = m.group(1)
        if name == '<...>':
          name = '<' + str(tid) + '>'
        thread_names[tid] = name
      return name + '-' + m.group(2)
    else:
      return m.group(0)

  # matches something like:
  # Binder_2-895, or com.google.android.inputmethod.latin-1078 etc...
  trace_data = re.sub(r'^\s*(\S+)-(\d+)', repl, trace_data,
                      flags=re.MULTILINE)
  return trace_data


def fix_missing_tgids(trace_data, pid2_tgid):
  """Replaces missing TGIDs from the trace data with those found in procfs

  Args:
    trace_data: the atrace data

  Returns:
    The updated trace data with missing TGIDs replaced with the correct TGID
  """

  def repl(m):
    tid = m.group(2)
    if (int(tid) > 0 and m.group(1) != '<idle>' and m.group(3) == '(-----)'
        and tid in pid2_tgid):
      # returns Proc_name-PID (TGID)
      # Binder_2-381 (-----) becomes Binder_2-381 (128)
      return m.group(1) + '-' + m.group(2) + ' ( ' + pid2_tgid[tid] + ')'

    return m.group(0)

  # matches something like:
  # Binder_2-895 (-----)
  trace_data = re.sub(r'^\s*(\S+)-(\d+)\s+(\(\S+\))', repl, trace_data,
                      flags=re.MULTILINE)
  return trace_data


def fix_circular_traces(out):
  """Fix inconsistentcies in traces due to circular buffering.

  The circular buffers are kept per CPU, so it is not guaranteed that the
  beginning of a slice is overwritten before the end. To work around this, we
  throw away the prefix of the trace where not all CPUs have events yet.

  Args:
    out: The data to fix.
  Returns:
    The updated trace data.
  """
  # If any of the CPU's buffers have filled up and
  # older events have been dropped, the kernel
  # emits markers of the form '##### CPU 2 buffer started ####' on
  # the line before the first event in the trace on that CPU.
  #
  # No such headers are emitted if there were no overflows or the trace
  # was captured with non-circular buffers.
  buffer_start_re = re.compile(r'^#+ CPU \d+ buffer started', re.MULTILINE)

  start_of_full_trace = 0

  while True:
    result = buffer_start_re.search(out, start_of_full_trace + 1)
    if result:
      start_of_full_trace = result.start()
    else:
      break

  if start_of_full_trace > 0:
    # Need to keep the header intact to make the importer happy.
    end_of_header = re.search(r'^[^#]', out, re.MULTILINE).start()
    out = out[:end_of_header] + out[start_of_full_trace:]
  return out

def do_preprocess_adb_cmd(command, serial):
  """Run an ADB command for preprocessing of output.

  Run an ADB command and get the results. This function is used for
  running commands relating to preprocessing of output data.

  Args:
      command: Command to run.
      serial: Serial number of device.
  """

  args = [command]
  dump, ret_code = util.run_adb_shell(args, serial)
  if ret_code != 0:
    return None

  dump = ''.join(dump)
  return dump

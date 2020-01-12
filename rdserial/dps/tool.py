# rdserialtool
# Copyright (C) 2019 Ryan Finnie
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.

import logging
import json
import datetime
import time
import statistics

from rdserial import __version__
import rdserial.dps
import rdserial.modbus


dps_supported_devices = ['dps', 'dps3005', 'dps5005', 'dps5015', 'dps5020', 'dps8005', 'dph5005']
rd_supported_devices = ['rd', 'rd6006']
supported_devices = dps_supported_devices + rd_supported_devices


class Tool:
    def __init__(self, parent=None):
        self.trends = {}
        if parent is not None:
            self.args = parent.args
            self.socket = parent.socket

    def trend_s(self, name, value):
        if not self.args.watch:
            return ''

        if name in self.trends:
            trend = statistics.mean(self.trends[name])
            self.trends[name] = self.trends[name][1:] + [value]
            if value > trend:
                return '\u2197'
            elif value < trend:
                return '\u2198'
            else:
                return ' '
        else:
            self.trends[name] = [value for x in range(self.args.trend_points)]
            return ' '

    def send_commands(self):
        register_commands = {}

        device_state = self.device_state_class()
        command_map = (
            ('set_volts', 'setting_volts'),
            ('set_amps', 'setting_amps'),
            ('set_output_state', 'output_state'),
            ('set_key_lock', 'key_lock'),
            ('set_brightness', 'brightness'),
            ('load_group', 'group_loader'),
        )
        group_command_map = (
            ('set_group_volts', 'setting_volts'),
            ('set_group_amps', 'setting_amps'),
            ('set_group_cutoff_volts', 'cutoff_volts'),
            ('set_group_cutoff_amps', 'cutoff_amps'),
        )
        if self.device_mode == 'dps':
            group_command_map += (
                ('set_group_cutoff_watts', 'cutoff_watts'),
                ('set_group_brightness', 'brightness'),
                ('set_group_maintain_output', 'maintain_output'),
                ('set_group_poweron_output', 'poweron_output'),
            )

        for arg_name, register_name in command_map:
            arg_val = getattr(self.args, arg_name)
            if arg_val is None:
                continue
            translation = device_state.register_properties[register_name]['to_int']
            description = device_state.register_properties[register_name]['description']
            register_num = device_state.register_properties[register_name]['register']
            register_val = translation(arg_val)
            logging.info('Setting "{}" to {}'.format(
                description, arg_val
            ))
            logging.debug('{} "{}" (register {}): {} ({})'.format(
                register_name, description, register_num, arg_val, register_val
            ))
            register_commands[register_num] = register_val

        if getattr(self.args, 'set_clock'):
            logging.info('Setting device clock')
            now = datetime.datetime.now()
            for name in ('year', 'month', 'day', 'hour', 'minute', 'second'):
                register_name = 'datetime_{}'.format(name)
                val = getattr(now, name)
                translation = device_state.register_properties[register_name]['to_int']
                description = device_state.register_properties[register_name]['description']
                register_num = device_state.register_properties[register_name]['register']
                register_val = translation(val)
                logging.debug('{} "{}" (register {}): {} ({})'.format(
                    register_name, description, register_num, val, register_val
                ))
                register_commands[register_num] = register_val

        if self.args.all_groups:
            groups = range(10)
        elif self.args.group is not None:
            groups = self.args.group
        else:
            groups = []
        for group in groups:
            device_group_state = self.device_group_state_class(group)

            for arg_name, register_name in group_command_map:
                arg_val = getattr(self.args, arg_name)
                if arg_val is None:
                    continue
                translation = device_group_state.register_properties[register_name]['to_int']
                description = device_group_state.register_properties[register_name]['description']
                register_num = device_group_state.register_properties[register_name]['register']
                register_val = translation(arg_val)
                logging.info('Setting group {} "{}" to {}'.format(
                    group, description, arg_val
                ))
                logging.debug('Group {} {} "{}" (register {}): {} ({})'.format(
                    group, register_name, description, register_num, arg_val, register_val
                ))
                register_commands[register_num] = register_val

        if len(register_commands) > 0:
            logging.info('')

        # Optimize into a set of minimal register writes
        register_commands_opt = {}
        for register in sorted(register_commands.keys()):
            found_opt = False
            for g in register_commands_opt:
                if (register == g + len(register_commands_opt[g])) and (len(register_commands_opt[g]) < 32):
                    register_commands_opt[g].append(register_commands[register])
                    found_opt = True
                    break
            if not found_opt:
                register_commands_opt[register] = [register_commands[register]]
        for register_base in register_commands_opt:
            logging.debug('Writing {} register(s) ({}) at base {}'.format(
                len(register_commands_opt[register_base]),
                register_commands_opt[register_base],
                register_base,
            ))
            self.modbus_client.write_registers(
                register_base, register_commands_opt[register_base], unit=self.args.modbus_unit,
            )

    def send_command(self, command, argument=None):
        register_commands = {}
        if command == "power_on":
            register_commands[0x12] = 1

        if command == "power_off":
            register_commands[0x12] = 0

        if command == "set_volts":
            register_commands[0x08] = argument

        if command == "set_current":
            register_commands[0x09] = argument

        # Optimize into a set of minimal register writes
        register_commands_opt = {}
        for register in sorted(register_commands.keys()):
            found_opt = False
            for g in register_commands_opt:
                if (register == g + len(register_commands_opt[g])) and (len(register_commands_opt[g]) < 32):
                    register_commands_opt[g].append(register_commands[register])
                    found_opt = True
                    break
            if not found_opt:
                register_commands_opt[register] = [register_commands[register]]
        for register_base in register_commands_opt:
            logging.info('Writing {} register(s) ({}) at base {}'.format(
                len(register_commands_opt[register_base]),
                register_commands_opt[register_base],
                register_base,
            ))
            self.modbus_client.write_registers(
                register_base, register_commands_opt[register_base], unit=self.args.modbus_unit,
            )

    def print_human(self, device_state):
        protection_map = {
            rdserial.dps.PROTECTION_GOOD: 'good',
            rdserial.dps.PROTECTION_OV: 'over-voltage',
            rdserial.dps.PROTECTION_OC: 'over-current',
            rdserial.dps.PROTECTION_OP: 'over-power',
        }
        print('Setting: {:5.02f}V, {:6.03f}A ({})'.format(
            device_state.setting_volts,
            device_state.setting_amps,
            ('CC' if device_state.constant_current else 'CV'),
        ))
        print('Output {:5}: {:5.02f}V{}, {:5.02f}A{}, {:6.02f}W{}'.format(
            ('(on)' if device_state.output_state else '(off)'),
            device_state.volts,
            self.trend_s('volts', device_state.volts),
            device_state.amps,
            self.trend_s('amps', device_state.amps),
            device_state.watts,
            self.trend_s('watts', device_state.watts),
        ))
        print('Input: {:5.02f}V{}, protection: {}'.format(
            device_state.input_volts,
            self.trend_s('input_volts', device_state.input_volts),
            protection_map[device_state.protection],
        ))
        print('Brightness: {}/5, key lock: {}'.format(
            device_state.brightness,
            'on' if device_state.key_lock else 'off',
        ))
        if hasattr(device_state, 'serial'):
            print('Model: {}, firmware: {}, serial: {}'.format(device_state.model, device_state.firmware, device_state.serial))
        else:
            print('Model: {}, firmware: {}'.format(device_state.model, device_state.firmware))
        print('Collection time: {}'.format(device_state.collection_time))
        if len(device_state.groups) > 0:
            print()
        for group, device_group_state in sorted(device_state.groups.items()):
            print('Group {}:'.format(group))
            print('    Setting: {:5.02f}V, {:6.03f}A'.format(device_group_state.setting_volts, device_group_state.setting_amps))
            if hasattr(device_group_state, 'cutoff_watts'):
                print('    Cutoff: {:5.02f}V, {:6.03f}A, {:5.01f}W'.format(
                    device_group_state.cutoff_volts,
                    device_group_state.cutoff_amps,
                    device_group_state.cutoff_watts,
                ))
            else:
                print('    Cutoff: {:5.02f}V, {:6.03f}A'.format(
                    device_group_state.cutoff_volts,
                    device_group_state.cutoff_amps,
                ))
            if hasattr(device_group_state, 'brightness'):
                print('    Brightness: {}/5'.format(device_group_state.brightness))
            if hasattr(device_group_state, 'maintain_output'):
                print('    Maintain output state: {}'.format(device_group_state.maintain_output))
            if hasattr(device_group_state, 'poweron_output'):
                print('    Output on power-on: {}'.format(device_group_state.poweron_output))

    def print_json(self, device_state):
        out = {x: getattr(device_state, x) for x in device_state.register_properties}
        out['collection_time'] = (device_state.collection_time - datetime.datetime.fromtimestamp(0)).total_seconds()
        out['groups'] = {}
        for group, device_group_state in device_state.groups.items():
            out['groups'][group] = {x: getattr(device_group_state, x) for x in device_group_state.register_properties}
        print(json.dumps(out, sort_keys=True))

    def assemble_device_state(self):
        device_state = self.device_state_class()
        registers_length = (85 if self.device_mode == 'rd' else 13)
        registers = self.modbus_client.read_registers(
            0x00, registers_length, unit=self.args.modbus_unit,
        )
        device_state.load(registers)

        if self.args.all_groups:
            groups = range(10)
        elif self.args.group is not None:
            groups = self.args.group
        else:
            groups = []
        for group in groups:
            register_offset = (0x04 if self.device_mode == 'rd' else 0x10)
            device_group_state = self.device_group_state_class(group)
            registers = self.modbus_client.read_registers(
                0x50 + (register_offset * group),
                len(device_group_state.register_properties),
                unit=self.args.modbus_unit,
            )
            device_group_state.load(registers, offset=(0x50 + (register_offset * group)))
            device_state.groups[group] = device_group_state

        return device_state

    def loop(self):
        while True:
            try:
                device_state = self.assemble_device_state()
                if self.args.json:
                    self.print_json(device_state)
                else:
                    self.print_human(device_state)
            except KeyboardInterrupt:
                raise
            except Exception:
                if self.args.watch:
                    logging.exception('An exception has occurred')
                else:
                    raise
            if self.args.watch:
                if not self.args.json:
                    print()
                time.sleep(self.args.watch_seconds)
            else:
                return

    def guiloop(self):
        while True:
            try:
                device_state = self.assemble_device_state()
                device_state.key_lock = 'off'
                if device_state.output_state:
                    self.watts_label.configure(text="{:5.02f}W".format(device_state.watts).replace(".",chr(0xb7)),foreground='#00FF00')
                    self.voltage_label.configure(text="{:5.03f}V".format(device_state.volts).replace(".",chr(0xb7)),foreground='#00FF00')
                    self.current_label.configure(text="{:5.03f}A".format(device_state.amps/10).replace(".",chr(0xb7)),foreground='#00FF00')
                    if device_state.constant_current:
                        self.power_label.configure(text="ON (CC)",foreground='#00FF00')
                    else:
                        self.power_label.configure(text="ON (CV)",foreground='#00FF00')
                else:
                    self.watts_label.configure(text="{:5.02f}V".format(device_state.input_volts).replace(".",chr(0xb7)),foreground='#AF69EE')
                    self.voltage_label.configure(text="{:5.03f}V".format(device_state.setting_volts).replace(".",chr(0xb7)),foreground='#FFD300')
                    self.current_label.configure(text="{:5.03f}A".format(device_state.setting_amps).replace(".",chr(0xb7)),foreground='#FFD300')
                    self.power_label.configure(text="OFF",foreground='#FF0000')
                if self.args.gui_on_top:
                    self.root.lift()
                self.root.update_idletasks()
                self.root.update()
            except Exception:
                if self.args.watch:
                    logging.exception('An exception has occurred')
                else:
                    return

    def toggle_power(self, event):
        device_state = self.assemble_device_state()
        if device_state.output_state:
            self.send_command('power_off')
        else:
            self.send_command('power_on')

    def voltage_up(self, event):
        device_state = self.assemble_device_state()
        if device_state.output_state:
            return
        else:
            new_voltage = int(device_state.setting_volts*100)+10
            self.send_command('set_volts', argument=new_voltage)

    def voltage_down(self, event):
        device_state = self.assemble_device_state()
        if device_state.output_state:
            return
        else:
            new_voltage = int(device_state.setting_volts*100)-10
            self.send_command('set_volts', argument=new_voltage)

    def current_up(self, event):
        device_state = self.assemble_device_state()
        if device_state.output_state:
            return
        else:
            new_current = int(device_state.setting_amps*1000)+100
            self.send_command('set_current', argument=new_current)

    def current_down(self, event):
        device_state = self.assemble_device_state()
        if device_state.output_state:
            return
        else:
            new_current = int(device_state.setting_amps*1000)-100
            self.send_command('set_current', argument=new_current)


    def main(self):
        if self.args.device in rd_supported_devices:
            self.device_mode = 'rd'
            self.device_state_class = rdserial.dps.RDDeviceState
            self.device_group_state_class = rdserial.dps.RDGroupState
        else:
            self.device_mode = 'dps'
            self.device_state_class = rdserial.dps.DPSDeviceState
            self.device_group_state_class = rdserial.dps.DPSGroupState
        self.modbus_client = rdserial.modbus.RTUClient(
            self.socket,
            baudrate=self.args.baud,
        )
        if self.args.gui:
            import tkinter as tk
            import pygubu
            self.root = tk.Tk()
            self.root.title('rdserialtool {}'.format(__version__))
            self.root.resizable(width=False, height=False)
            self.builder = pygubu.Builder()
            self.builder.add_from_file('rdserial/gui.ui')
            self.main_window = self.builder.get_object('output', self.root)
            self.voltage_label = self.builder.get_object('voltage')
            self.current_label = self.builder.get_object('current')
            self.voltage_label.bind('<Button-1>',self.toggle_power)
            self.voltage_label.bind('<Button-4>', self.voltage_up)
            self.voltage_label.bind('<Button-5>', self.voltage_down)
            self.current_label.bind('<Button-1>',self.toggle_power)
            self.current_label.bind('<Button-4>', self.current_up)
            self.current_label.bind('<Button-5>', self.current_down)
            self.watts_label = self.builder.get_object('watts')
            self.power_label = self.builder.get_object('power')
            self.loop = self.guiloop
        try:
            self.send_commands()
            self.loop()
        except KeyboardInterrupt:
            pass

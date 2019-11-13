# coding: utf-8

""" Provision Step Classes """

import tmt
import os

from click import echo

from tmt.utils import SpecificationError
from tmt.steps.provision import vagrant, localhost


class Provision(tmt.steps.Step):
    """ Provision step """
    name = 'provision'

    # supported provisioners are not loaded automatically, import them and map them in how_map
    how_map = {
        'vagrant': vagrant.ProvisionVagrant,
        'libvirt': vagrant.ProvisionVagrant,
        'virtual': vagrant.ProvisionVagrant,
        'local': localhost.ProvisionLocalhost,
        'localhost': localhost.ProvisionLocalhost
    }

    # default provisioner
    how = 'virtual'

    def __init__(self, data, plan):
        # List of provisioned guests
        self.guests = []

        # Parent
        self.super = super(Provision, self)

        # Initialize parent
        self.super.__init__(data, plan)

    def _check_data(self):
        """ Validate input data """

        # if not specified, use 'virtual' provisioner as default
        for data in self.data:
            how = data['how']
            # is how supported?
            if how not in self.how_map:
                raise tmt.utils.SpecificationError("How '{}' in plan '{}' is not implemented".format(how, self.plan))

    def wake(self):
        """ Wake up the step (process workdir and command line) """
        super(Provision, self).wake()

        self._check_data()
        image = self.opt('image')

        # Add plugins for all guests
        for data in self.data:
            if image:
                data['image'] = image
            self.guests.append(self.how_map[data['how']](data, self))

    def go(self):
        """ Provision all resources """
        self.super.go()

        for guest in self.guests:
            guest.go()
            # this has to be fixed first
            #guest.save()

    def execute(self, *args, **kwargs):
        for guest in self.guests:
            guest.execute(*args, **kwargs)

    def load(self):
        self.guests = self.read(self.guests)

        for guest in self.guests:
            guest.load()

    def save(self):
        self.write(self.guests)

        for guest in self.guests:
            guest.save()

    def show(self):
        """ Show provision details """
        keys = ['how', 'image']
        super(Provision, self).show(keys)

    def sync_workdir_to_guest(self):
        for guest in self.guests:
            guest.sync_workdir_to_guest()

    def sync_workdir_from_guest(self):
        for guest in self.guests:
            guest.sync_workdir_from_guest()

    def copy_from_guest(self, target):
        for guest in self.guests:
            guest.copy_from_guest(target)

    def destroy(self):
        for guest in self.guests:
            guest.destroy()

    def prepare(self, how, what):
        for guest in self.guests:
            guest.prepare(how, what)

    def clean(self):
        for guest in self.guests:
            guest.clean()

    def write(self, data):
        path = os.path.join(self.workdir, 'guests.yaml')
        self.super.write(path, self.dictionary_to_yaml(data))

    def read(self, current):
        path = os.path.join(self.workdir, 'guests.yaml')
        if os.path.exists(path) and os.path.isfile(path):
            return self.super.read(path)
        else:
            return current

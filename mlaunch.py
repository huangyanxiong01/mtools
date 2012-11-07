#!/usr/bin/python

from pymongo import Connection
from pymongo.connection import AutoReconnect
from pymongo.errors import OperationFailure
import subprocess
import argparse
import threading
import os, time


def pingMongoD(host, interval=1, timeout=30):
	con = None
	startTime = time.time()
	while not con:
		if (time.time() - startTime) > timeout:
			return False
		try:
			con = Connection(host)
			return True
		except AutoReconnect, e:
			time.sleep(1)


class MongoLauncher(object):

	def __init__(self):
		self.parseArgs()
		self.launch()

	def parseArgs(self):
		# create parser object
		parser = argparse.ArgumentParser(description='script to launch MongoDB stand-alone servers, replica sets, and shards')
		
		# positional argument
		parser.add_argument('dir', action='store', nargs='?', const='.', default='.', help='base directory to create db and log paths')

		# either single or replica set
		me_group = parser.add_mutually_exclusive_group(required=True)
		me_group.add_argument('--single', action='store_true', help='creates a single stand-alone mongod instance')
		me_group.add_argument('--replicaset', action='store_true', help='creates replica set with several mongod instances')
		
		parser.add_argument('--nodes', action='store', metavar='NUM', type=int, default=3, help='adds NUM data nodes to replica set (requires --replicaset)')
		parser.add_argument('--arbiter', action='store_true', default=False, help='adds arbiter to replica set (requires --replicaset)')
		parser.add_argument('--name', action='store', metavar='NAME', default='default', help='name for replica set')
		
		# sharded or not
		parser.add_argument('--sharded', action='store', nargs='*', metavar='NAME', help='creates a sharded setup consisting of several singles or replica sets')
		parser.add_argument('--config', action='store', default=1, type=int, metavar='NUM', choices=[1, 3], help='adds NUM config servers to sharded setup (NUM must be 1 or 3, requires --sharded)')

		# verbose, port, mongo
		parser.add_argument('--port', action='store', type=int, default=27017, help='port for mongod, start of port range in case of replica set or shards')
		parser.add_argument('--mongo', action='store_true', default=False, help='start mongo shell and connect to mongod (--single), primary mongod (--replicaset), or mongos (--sharded)')
		parser.add_argument('--verbose', action='store_true', default=False, help='outputs information about the launch')

		self.args = vars(parser.parse_args())
		print self.args



	def launch(self):
		if self.args['sharded']:
			self._launchSharded()
		elif self.args['single']:
			self._launchSingle(self.args['dir'], self.args['port'], verbose=self.args['verbose'])
		elif self.args['replicaset']:
			self._launchReplSet(self.args['dir'], self.args['port'], self.args['name'], self.args['nodes'], self.args['arbiter'], self.args['verbose'])


	def _createPaths(self, basedir, name=None, verbose=False):
		if name:
			datapath = os.path.join(basedir, 'data', name)
		else:
			datapath = os.path.join(basedir, 'data')

		dbpath = os.path.join(datapath, 'db')
		if not os.path.exists(dbpath):
			os.makedirs(dbpath)
		if verbose:
			print 'creating directory: %s'%dbpath
		
		return datapath


	def _launchSharded(self):
		# start up shards
		if len(self.args['sharded']) == 1:
			try:
				# --sharded was a number, name shards shard01, shard02, ...
				n_shards = int(self.args['sharded'][0])
				shard_names = ['shard%.2i'%(i+1) for i in range(n_shards)]
			except ValueError, e:
				# --sharded was a string, use it as name for the one shard 
				shard_names = self.args['sharded']
		else:
			shard_names = self.args['sharded']

		nextport = self.args['port']
		for p, shard in enumerate(shard_names):
			if self.args['single']:
				self._launchSingle(self.args['dir'], nextport, name=shard, verbose=self.args['verbose'])
				nextport += 1
			elif self.args['replicaset']:
				self._launchReplSet(self.args['dir'], nextport, shard, self.args['nodes'], self.args['arbiter'], self.args['verbose'])
				nextport += self.args['nodes']
				if self.args['arbiter']:
					nextport += 1

		
		# start up config server(s)
		config_string = []
		if self.args['config'] == 1:
			config_names = ['config']
		else:
			config_names = ['config1', 'config2', 'config3']
			
		for name in config_names:
			self._launchConfig(self.args['dir'], nextport, name, verbose=self.args['verbose'])
			config_string.append('127.0.0.1:%i'%nextport)
			nextport += 1
		
		# start up mongos
		self._launchMongoS(os.path.join(self.args['dir'], 'data', 'mongos.log'), nextport, ','.join(config_string), verbose=self.args['verbose'])

		# TODO: configure shards



	def _launchReplSet(self, basedir, portstart, name, numdata, arbiter, verbose=False):
		threads = []
		configDoc = {'_id':name, 'members':[]}

		for i in range(numdata):
			datapath = self._createPaths(basedir, '%s/rs%i'%(name, i+1), verbose)
			self._launchMongoD(os.path.join(datapath, 'db'), os.path.join(datapath, 'mongod.log'), portstart+i, replset=name, verbose=verbose)
		
			host = '127.0.0.1:%i'%(portstart+i)
			configDoc['members'].append({'_id':len(configDoc['members']), 'host':host})
			threads.append(threading.Thread(target=pingMongoD, args=(host, 1, 30)))
			if verbose:
				print "waiting for mongod at %s to start up..."%host

		# launch arbiter if True
		if arbiter:
			datapath = self._createPaths(basedir, '%s/arb'%(name), verbose)
			self._launchMongoD(os.path.join(datapath, 'db'), os.path.join(datapath, 'mongod.log'), portstart+numdata, replset=name, verbose=verbose)
			
			host = '127.0.0.1:%i'%(portstart+numdata)
			configDoc['members'].append({'_id':len(configDoc['members']), 'host':host, 'arbiterOnly': True})
			threads.append(threading.Thread(target=pingMongoD, args=(host, 1, 30)))
			if verbose:
				print "waiting for mongod at %s to start up..."%host

		for thread in threads:
			thread.start()

		for thread in threads:
			thread.join()

		if verbose:
			print "all mongod processes running."

		# initiate replica set
		con = Connection('127.0.0.1:%i'%portstart)
		try:
			rs_status = con['admin'].command({'replSetGetStatus': 1})
		except OperationFailure, e:
			con['admin'].command({'replSetInitiate':configDoc})
			if verbose:
				print "replica set configured."



	def _launchConfig(self, basedir, port, name=None, verbose=False):
		datapath = self._createPaths(basedir, name, verbose)
		self._launchMongoD(os.path.join(datapath, 'db'), os.path.join(datapath, 'mongod.log'), port, replset=None, verbose=verbose, extra='--configsvr')

		host = '127.0.0.1:%i'%port
		t = threading.Thread(target=pingMongoD, args=(host, 1, 30))
		t.start()
		if verbose:
			print "waiting for mongod at %s to start up..."%host
		t.join()
		if verbose:
			print "running."


	def _launchSingle(self, basedir, port, name=None, verbose=False):
		datapath = self._createPaths(basedir, name, verbose)
		self._launchMongoD(os.path.join(datapath, 'db'), os.path.join(datapath, 'mongod.log'), port, replset=None, verbose=verbose)

		host = '127.0.0.1:%i'%port
		t = threading.Thread(target=pingMongoD, args=(host, 1, 30))
		t.start()
		if verbose:
			print "waiting for mongod at %s to start up..."%host
		t.join()
		if verbose: 
			print "running."

	def _launchMongoD(self, dbpath, logpath, port, replset=None, verbose=False, extra=''):
		if replset:
			rs_param = '--replSet %s'%replset
		else:
			rs_param = ''

		ret = subprocess.call(['mongod %s --dbpath %s --logpath %s --port %i --logappend %s --fork'%(rs_param, dbpath, logpath, port, extra)], shell=True)
		if verbose:
			print 'launching: mongod %s --dbpath %s --logpath %s --port %i --logappend %s --fork'%(rs_param, dbpath, logpath, port, extra)


	def _launchMongoS(self, logpath, port, configdb, verbose=False):
		ret = subprocess.call(['mongos --logpath %s --port %i --configdb %s --logappend --fork'%(logpath, port, configdb)], shell=True)
		if verbose:
			print 'launching: mongos --logpath %s --port %i --configdb %s --logappend --fork'%(logpath, port, configdb)
		
		host = '127.0.0.1:%i'%port
		t = threading.Thread(target=pingMongoD, args=(host, 1, 30))
		t.start()
		if verbose:
			print "waiting for mongos at %s to start up..."%host
		t.join()
		if verbose:
			print "running."




if __name__ == '__main__':
	mongoLauncher = MongoLauncher()



"""
mongolaunch --single name --port 30000 --mongo .

	* creates
		./data/name/db
        ./data/name/logs/mongod.log
	* starts mongod
	* checks when mongod is ready
	* starts mongo

mongolaunch --replicaset --nodes 3 --arbiter --port 20000 --mongo .

	* creates for each member
		./data/name/rs<x>/db
        ./data/name/rs<x>/logs/mongod.log
	* starts all mongod
	* checks when all mongod are ready
	* starts mongo and connects to primary

mongolaunch --sharded name1 name2 ...
"""
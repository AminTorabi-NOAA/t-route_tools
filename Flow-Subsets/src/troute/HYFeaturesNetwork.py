from .AbstractNetwork import AbstractNetwork
import pandas as pd
import numpy as np
import geopandas as gpd
import time
import json
from pathlib import Path
import pyarrow.parquet as pq
from itertools import chain
from joblib import delayed, Parallel
from collections import defaultdict

import troute.nhd_io as nhd_io #FIXME
from troute.nhd_network import reverse_dict, extract_connections, reverse_network, reachable

__verbose__ = False
__showtiming__ = False

def read_geopkg(file_path, data_assimilation_parameters, waterbody_parameters):
    # Establish which layers we will read. We'll always need the flowpath tables
    layers = ['flowpaths','flowpath_attributes']

    # If waterbodies are being simulated, read lakes table
    if waterbody_parameters.get('break_network_at_waterbodies',False):
        layers.append('lakes')

    # If any DA is activated, read network table as well for gage information
    streamflow_nudging = data_assimilation_parameters.get('streamflow_da',{}).get('streamflow_nudging',False)
    usgs_da = data_assimilation_parameters.get('reservoir_da',{}).get('reservoir_persistence_usgs',False)
    usace_da = data_assimilation_parameters.get('reservoir_da',{}).get('reservoir_persistence_usace',False)
    rfc_da = waterbody_parameters.get('rfc',{}).get('reservoir_rfc_forecasts',False)
    if any([streamflow_nudging, usgs_da, usace_da, rfc_da]):
        layers.append('network')
    
    # Retrieve geopackage information:
    with Parallel(n_jobs=len(layers)) as parallel:
        jobs = []
        for layer in layers:
            jobs.append(
                delayed(gpd.read_file)
                #(f, qlat_file_value_col, gw_bucket_col, terrain_ro_col)
                #delayed(nhd_io.get_ql_from_csv)
                (filename=file_path, layer=layer)                    
            )
        gpkg_list = parallel(jobs)
    table_dict = {layers[i]: gpkg_list[i] for i in range(len(layers))}
    flowpaths = pd.merge(table_dict.get('flowpaths'), table_dict.get('flowpath_attributes'), on='id')
    lakes = table_dict.get('lakes', pd.DataFrame())
    network = table_dict.get('network', pd.DataFrame())

    '''
    flowpaths = gpd.read_file(file_path, layer='flowpaths')
    flowpath_attributes = gpd.read_file(file_path, layer='flowpath_attributes')
    flowpaths = pd.merge(flowpaths, flowpath_attributes, on='id')
    '''

    return flowpaths, lakes, network

def read_json(file_path, edge_list):
    dfs = []
    with open(edge_list) as edge_file:
        edge_data = json.load(edge_file)
        edge_map = {}
        for id_dict in edge_data:
            edge_map[ id_dict['id'] ] = id_dict['toid']
        with open(file_path) as data_file:
            json_data = json.load(data_file)  
            for key_wb, value_params in json_data.items():
                df = pd.json_normalize(value_params)
                df['id'] = key_wb
                df['toid'] = edge_map[key_wb]
                dfs.append(df)
        df_main = pd.concat(dfs, ignore_index=True)

    return df_main

def numeric_id(flowpath):
    id = flowpath['id'].split('-')[-1]
    toid = flowpath['toid'].split('-')[-1]
    flowpath['id'] = int(float(id))
    flowpath['toid'] = int(float(toid))
    return flowpath

def read_ngen_waterbody_df(parm_file, lake_index_field="wb-id", lake_id_mask=None):
    """
    Reads .gpkg or lake.json file and prepares a dataframe, filtered
    to the relevant reservoirs, to provide the parameters
    for level-pool reservoir computation.
    """
    def node_key_func(x):
        return int( x.split('-')[-1] )
    if Path(parm_file).suffix=='.gpkg':
        df = gpd.read_file(parm_file, layer='lakes')

        df = (
            df.drop(['id','toid','hl_id','hl_reference','hl_uri','geometry'], axis=1)
            .rename(columns={'hl_link': 'lake_id'})
            )
        df['lake_id'] = df.lake_id.astype(float).astype(int)
        df = df.set_index('lake_id').drop_duplicates().sort_index()
    elif Path(parm_file).suffix=='.json':
        df = pd.read_json(parm_file, orient="index")
        df.index = df.index.map(node_key_func)
        df.index.name = lake_index_field

    if lake_id_mask:
        df = df.loc[lake_id_mask]
    return df

def read_ngen_waterbody_type_df(parm_file, lake_index_field="wb-id", lake_id_mask=None):
    """
    """
    #FIXME: this function is likely not correct. Unclear how we will get 
    # reservoir type from the gpkg files. Information should be in 'crosswalk'
    # layer, but as of now (Nov 22, 2022) there doesn't seem to be a differentiation
    # between USGS reservoirs, USACE reservoirs, or RFC reservoirs...
    def node_key_func(x):
        return int( x.split('-')[-1] )
    
    if Path(parm_file).suffix=='.gpkg':
        df = gpd.read_file(parm_file, layer="crosswalk").set_index('id')
    elif Path(parm_file).suffix=='.json':
        df = pd.read_json(parm_file, orient="index")

    df.index = df.index.map(node_key_func)
    df.index.name = lake_index_field
    if lake_id_mask:
        df = df.loc[lake_id_mask]
        
    return df

def read_geo_file(supernetwork_parameters, waterbody_parameters, data_assimilation_parameters):
        
    geo_file_path = supernetwork_parameters["geo_file_path"]
    
    file_type = Path(geo_file_path).suffix
    if(  file_type == '.gpkg' ):        
        flowpaths, lakes, network = read_geopkg(geo_file_path, 
                                                data_assimilation_parameters,
                                                waterbody_parameters)
        #TODO Do we need to keep .json as an option?
        '''
        elif( file_type == '.json') :
            edge_list = supernetwork_parameters['flowpath_edge_list']
            self._dataframe = read_json(geo_file_path, edge_list) 
        '''
    else:
        raise RuntimeError("Unsupported file type: {}".format(file_type))
    
    return flowpaths, lakes, network

def load_bmi_data(value_dict, bmi_parameters,): 
    # Get the column names that we need from each table of the geopackage
    flowpath_columns = bmi_parameters.get('flowpath_columns')
    attributes_columns = bmi_parameters.get('attributes_columns')
    lakes_columns = bmi_parameters.get('waterbody_columns')
    network_columns = bmi_parameters.get('network_columns')

    # Create dataframes with the relevent columns
    flowpaths = pd.DataFrame(data=None, columns=flowpath_columns)
    for col in flowpath_columns:
        flowpaths[col] = value_dict[col]

    flowpath_attributes = pd.DataFrame(data=None, columns=attributes_columns)
    for col in attributes_columns:
        flowpath_attributes[col] = value_dict[col]
    flowpath_attributes = flowpath_attributes.rename(columns={'attributes_id': 'id'})

    lakes = pd.DataFrame(data=None, columns=lakes_columns)
    for col in lakes_columns:
        lakes[col] = value_dict[col]

    network = pd.DataFrame(data=None, columns=network_columns)
    for col in network_columns:
        network[col] = value_dict[col]
    network = network.rename(columns={'network_id': 'id'})

    # Merge the two flowpath tables into one
    flowpaths = pd.merge(flowpaths, flowpath_attributes, on='id')

    return flowpaths, lakes, network


class HYFeaturesNetwork(AbstractNetwork):
    """
    
    """
    __slots__ = ["_upstream_terminal"]

    def __init__(self, 
                 supernetwork_parameters, 
                 waterbody_parameters,
                 data_assimilation_parameters,
                 restart_parameters, 
                 compute_parameters,
                 forcing_parameters,
                 hybrid_parameters, 
                 verbose=False, 
                 showtiming=False,
                 from_files=True,
                 value_dict={},
                 bmi_parameters={},):
        """
        
        """
        self.supernetwork_parameters = supernetwork_parameters
        self.waterbody_parameters = waterbody_parameters
        self.data_assimilation_parameters = data_assimilation_parameters
        self.restart_parameters = restart_parameters
        self.compute_parameters = compute_parameters
        self.forcing_parameters = forcing_parameters
        self.hybrid_parameters = hybrid_parameters
        self.verbose = verbose
        self.showtiming = showtiming

        if self.verbose:
            print("creating supernetwork connections set")
        if self.showtiming:
            start_time = time.time()
        
        #------------------------------------------------
        # Load hydrofabric information
        #------------------------------------------------
        if from_files:
            flowpaths, lakes, network = read_geo_file(
                self.supernetwork_parameters,
                self.waterbody_parameters,
                self.data_assimilation_parameters
            )
        else:
            flowpaths, lakes, network = load_bmi_data(
                value_dict, 
                bmi_parameters,
                )

        # Preprocess network objects
        self.preprocess_network(flowpaths)

        # Preprocess waterbody objects
        self.preprocess_waterbodies(lakes)

        # Preprocess data assimilation objects #TODO: Move to DataAssimilation.py?
        self.preprocess_data_assimilation(network)

        if self.verbose:
            print("supernetwork connections set complete")
        if self.showtiming:
            print("... in %s seconds." % (time.time() - start_time))
            

        super().__init__()   
            
        # Create empty dataframe for coastal_boundary_depth_df. This way we can check if
        # it exists, and only read in SCHISM data during 'assemble_forcings' if it doesn't
        self._coastal_boundary_depth_df = pd.DataFrame()

    def extract_waterbody_connections(rows, target_col, waterbody_null=-9999):
        """Extract waterbody mapping from dataframe.
        TODO deprecate in favor of waterbody_connections property"""
        return (
            rows.loc[rows[target_col] != waterbody_null, target_col].astype("int").to_dict()
        )

    @property
    def downstream_flowpath_dict(self):
        return self._flowpath_dict

    @property
    def waterbody_connections(self):
        """
            A dictionary where the keys are the reach/segment id, and the
            value is the id to look up waterbody parameters
        """
        return self._waterbody_connections
    
    @property
    def gages(self):
        """
        FIXME
        """
        return self._gages
    
    @property
    def waterbody_null(self):
        return np.nan #pd.NA
    
    def preprocess_network(self, flowpaths):
        self._dataframe = flowpaths

        # Don't need the string prefix anymore, drop it
        mask = ~ self.dataframe['toid'].str.startswith("tnex") 
        self._dataframe = self.dataframe.apply(numeric_id, axis=1)
        
        # handle segment IDs that are also waterbody IDs. The fix here adds a large value
        # to the segmetn IDs, creating new, unique IDs. Otherwise our connections dictionary
        # will get confused because there will be repeat IDs...
        duplicate_wb_segments = self.supernetwork_parameters.get("duplicate_wb_segments", None)
        duplicate_wb_id_offset = self.supernetwork_parameters.get("duplicate_wb_id_offset", 9.99e11)
        if duplicate_wb_segments:
            # update the values of the duplicate segment IDs
            fix_idx = self.dataframe.id.isin(set(duplicate_wb_segments))
            self._dataframe.loc[fix_idx,"id"] = (self.dataframe[fix_idx].id + duplicate_wb_id_offset).astype("int64")

        # make the flowpath linkage, ignore the terminal nexus
        self._flowpath_dict = dict(zip(self.dataframe.loc[mask].toid, self.dataframe.loc[mask].id))
        
        # **********  need to be included in flowpath_attributes  *************
        self._dataframe['alt'] = 1.0 #FIXME get the right value for this... 

        cols = self.supernetwork_parameters.get('columns',None)
        
        if cols:
            self._dataframe = self.dataframe[list(cols.values())]
            # Rename parameter columns to standard names: from route-link names
            #        key: "link"
            #        downstream: "to"
            #        dx: "Length"
            #        n: "n"  # TODO: rename to `manningn`
            #        ncc: "nCC"  # TODO: rename to `mannningncc`
            #        s0: "So"  # TODO: rename to `bedslope`
            #        bw: "BtmWdth"  # TODO: rename to `bottomwidth`
            #        waterbody: "NHDWaterbodyComID"
            #        gages: "gages"
            #        tw: "TopWdth"  # TODO: rename to `topwidth`
            #        twcc: "TopWdthCC"  # TODO: rename to `topwidthcc`
            #        alt: "alt"
            #        musk: "MusK"
            #        musx: "MusX"
            #        cs: "ChSlp"  # TODO: rename to `sideslope`
            self._dataframe = self.dataframe.rename(columns=reverse_dict(cols))
            self._dataframe.set_index("key", inplace=True)
            self._dataframe = self.dataframe.sort_index()

        # numeric code used to indicate network terminal segments
        terminal_code = self.supernetwork_parameters.get("terminal_code", 0)

        # There can be an externally determined terminal code -- that's this first value
        self._terminal_codes = set()
        self._terminal_codes.add(terminal_code)
        # ... but there may also be off-domain nodes that are not explicitly identified
        # but which are terminal (i.e., off-domain) as a result of a mask or some other
        # an interior domain truncation that results in a
        # otherwise valid node value being pointed to, but which is masked out or
        # being intentionally separated into another domain.
        self._terminal_codes = self.terminal_codes | set(
            self.dataframe[~self.dataframe["downstream"].isin(self.dataframe.index)]["downstream"].values
        )

        #This is NEARLY redundant to the self.terminal_codes property, but in this case
        #we actually need the mapping of what is upstream of that terminal node as well.
        #we also only want terminals that actually exist based on definition, not user input
        terminal_mask = ~self._dataframe["downstream"].isin(self._dataframe.index)
        terminal = self._dataframe.loc[ terminal_mask ]["downstream"]
        self._upstream_terminal = dict()
        for key, value in terminal.items():
            self._upstream_terminal.setdefault(value, set()).add(key)

        # build connections dictionary
        self._connections = extract_connections(
            self.dataframe, "downstream", terminal_codes=self.terminal_codes
        )

    def preprocess_waterbodies(self, lakes):
        # If waterbodies are being simulated, create waterbody dataframes and dictionaries
        if not lakes.empty:
            self._waterbody_df = (
                lakes[['hl_link','ifd','LkArea','LkMxE','OrificeA',
                       'OrificeC','OrificeE','WeirC','WeirE','WeirL']]
                .rename(columns={'hl_link': 'lake_id'})
                )
            self._waterbody_df['lake_id'] = self.waterbody_dataframe.lake_id.astype(float).astype(int)
            self._waterbody_df = self.waterbody_dataframe.set_index('lake_id').drop_duplicates().sort_index()
            
            # Create wbody_conn dictionary:
            #FIXME temp solution for missing waterbody info in hydrofabric
            self.bandaid()
            
            wbody_conn = self.dataframe[['waterbody']].dropna()
            wbody_conn = (
                wbody_conn['waterbody']
                .str.split(',',expand=True)
                .reset_index()
                .melt(id_vars='key')
                .drop('variable', axis=1)
                .dropna()
                .astype(int)
                )
            
            self._waterbody_connections = (
                wbody_conn[wbody_conn['value'].isin(self.waterbody_dataframe.index)]
                .set_index('key')['value']
                .to_dict()
                )
            
            self._dataframe = self.dataframe.drop('waterbody', axis=1)

            # if waterbodies are being simulated, adjust the connections graph so that 
            # waterbodies are collapsed to single nodes. Also, build a mapping between 
            # waterbody outlet segments and lake ids
            break_network_at_waterbodies = self.waterbody_parameters.get("break_network_at_waterbodies", False)
            if break_network_at_waterbodies:
                self._connections, self._link_lake_crosswalk = replace_waterbodies_connections(
                    self.connections, self.waterbody_connections
                )
            else:
                self._link_lake_crosswalk = None
            
            self._waterbody_types_df = pd.DataFrame(
                data = 1, 
                index = self.waterbody_dataframe.index, 
                columns = ['reservoir_type']).sort_index()
            
            self._waterbody_type_specified = True
            
        else:
            self._waterbody_df = pd.DataFrame()
            self._waterbody_types_df = pd.DataFrame()
            self._waterbody_connections = {}
            self._waterbody_type_specified = False
            self._link_lake_crosswalk = None

    def preprocess_data_assimilation(self, network):
        if not network.empty:
            gages_df = network[['id','hl_uri','hydroseq']].drop_duplicates()
            # clear out missing values
            gages_df = gages_df[~gages_df['hl_uri'].isnull()]
            gages_df = gages_df[~gages_df['hydroseq'].isnull()]
            # make 'id' an integer
            gages_df['id'] = gages_df['id'].str.split('-',expand=True).loc[:,1].astype(float).astype(int)
            # split the hl_uri column into type and value
            gages_df[['type','value']] = gages_df.hl_uri.str.split('-',expand=True,n=1)
            # filter for 'Gages' only
            gages_df = gages_df[gages_df['type'].isin(['Gages','NID'])]
            # In the event that a segment contains multiple gages, assign only the furthest 
            # downstream segment ID to any given gage
            gages_df = gages_df.sort_values(['value','hydroseq']).groupby('value').last().reset_index()
            # transform dataframe into a dictionary where key is segment ID and value is gage ID
            self._gages = gages_df[['id','value']].rename(columns={'value': 'gages'}).set_index('id').to_dict()

            # Create lake_gage crosswalk dataframes:
            link_gage_df = (
                pd.DataFrame.from_dict(self._gages)
                .reset_index()
                .rename(columns={'index': 'link'})
                .set_index('link')
                )
            lake_gage_df = (
                pd.DataFrame(self.waterbody_connections.items(),
                             columns = ['link', 'lake_id'])
                             .set_index('link')
                             .join(link_gage_df)
                             .reset_index()[['lake_id','gages']]
                )
            lake_gage_df = lake_gage_df[~lake_gage_df['gages'].isnull()]
            #################################
            #FIXME: temporary solution, this makes sure each lake only has one gage, and that
            # it is the gage furthest downstream
            lake_ids = lake_gage_df.lake_id.unique()
            lake_outflow_gage_ids = []

            for i in lake_ids:
                temp_df = lake_gage_df[lake_gage_df['lake_id']==i]
                lake_outflow_gage_ids.append(
                    gages_df[gages_df['value']
                             .isin(temp_df.gages)]
                             .sort_values('hydroseq')
                             .iloc[-1,:]
                             .value
                )

            lake_gage_df = lake_gage_df[lake_gage_df['gages'].isin(lake_outflow_gage_ids)]
            #####################################
            #FIXME: temporary solution, handles USGS and USACE reservoirs. Need to update for
            # RFC reservoirs...
            usgs_ind = lake_gage_df.gages.str.isnumeric()
            self._usgs_lake_gage_crosswalk = (
                lake_gage_df.loc[usgs_ind].rename(columns={'lake_id': 'usgs_lake_id', 'gages': 'usgs_gage_id'})
                .set_index('usgs_lake_id')
                )
            
            self._usace_lake_gage_crosswalk =  (
                lake_gage_df.loc[~usgs_ind].rename(columns={'lake_id': 'usace_lake_id', 'gages': 'usace_gage_id'})
                .set_index('usace_lake_id')
                )
            
            self._waterbody_types_df.loc[self._usgs_lake_gage_crosswalk.index,'reservoir_type'] = 2
            self._waterbody_types_df.loc[self._usace_lake_gage_crosswalk.index,'reservoir_type'] = 3
        else:
            self._gages = {}
            self._usgs_lake_gage_crosswalk = pd.DataFrame()
            self._usgs_lake_gage_crosswalk = pd.DataFrame()
    
    def build_qlateral_array(self, run,):
        
        # TODO: set default/optional arguments
        qts_subdivisions = run.get("qts_subdivisions", 1)
        nts = run.get("nts", 1)
        qlat_input_folder = run.get("qlat_input_folder", None)
        qlat_input_file = run.get("qlat_input_file", None)

        if qlat_input_folder:
            qlat_input_folder = Path(qlat_input_folder)
            if "qlat_files" in run:
                qlat_files = run.get("qlat_files")
                qlat_files = [qlat_input_folder.joinpath(f) for f in qlat_files]
            elif "qlat_file_pattern_filter" in run:
                qlat_file_pattern_filter = run.get(
                    "qlat_file_pattern_filter", "*CHRT_OUT*"
                )
                qlat_files = sorted(qlat_input_folder.glob(qlat_file_pattern_filter))

            dfs=[]
            for f in qlat_files:
                df = read_file(f).set_index(['feature_id']) 
                dfs.append(df)
            
            # lateral flows [m^3/s] are stored at NEXUS points with NEXUS ids
            nexuses_lateralflows_df = pd.concat(dfs, axis=1)  
            
            # Take flowpath ids entering NEXUS and replace NEXUS ids by the upstream flowpath ids
            qlats_df = pd.concat( (nexuses_lateralflows_df.loc[int(k)].rename(v)
                                for k,v in self.downstream_flowpath_dict.items() ), axis=1
                                ).T 
            qlats_df.columns=range(len(qlat_files))
            qlats_df = qlats_df[qlats_df.index.isin(self.segment_index)]

            '''
            #For a terminal nexus, we want to include the lateral flow from the catchment contributing to that nexus
            #one way to do that is to cheat and put that lateral flow at the upstream...this is probably the simplest way
            #right now.  The other is to create a virtual channel segment downstream to "route" i.e accumulate into
            #but it isn't clear right now how to do that with flow/velocity/depth requirements
            #find the terminal nodes
            for tnx, test_up in self._upstream_terminal.items():
                #first need to ensure there is an upstream location to dump to
                pdb.set_trace()
                for nex in test_up:
                    try:
                        #FIXME if multiple upstreams exist in this case then a choice is to be made as to which it goes into
                        #some cases the choice is easy cause the upstream doesn't exist, but in others, it may not be so simple
                        #in such cases where multiple valid upstream nexuses exist, perhaps the mainstem should be used?
                        pdb.set_trace()
                        qlats_df.loc[up] += nexuses_lateralflows_df.loc[tnx]
                        break #flow added, don't add it again!
                    except KeyError:
                        #this upstream doesn't actually exist on the network (maybe it is a headwater?)
                        #or perhaps the output file doesnt exist?  If this is the case, this isn't a good trap
                        #but for now, add the flow to a known good nexus upstream of the terminal
                        continue
                    #TODO what happens if can't put the qlat anywhere?  Right now this silently ignores the issue...
                qlats_df.drop(tnx, inplace=True)
            '''

            # The segment_index has the full network set of segments/flowpaths. 
            # Whereas the set of flowpaths that are downstream of nexuses is a 
            # subset of the segment_index. Therefore, all of the segments/flowpaths
            # that are not accounted for in the set of flowpaths downstream of
            # nexuses need to be added to the qlateral dataframe and padded with
            # zeros.
            all_df = pd.DataFrame( np.zeros( (len(self.segment_index), len(qlats_df.columns)) ), index=self.segment_index,
                columns=qlats_df.columns )
            all_df.loc[ qlats_df.index ] = qlats_df
            qlats_df = all_df.sort_index()

        elif qlat_input_file:
            qlats_df = nhd_io.get_ql_from_csv(qlat_input_file)
        else:
            qlat_const = run.get("qlat_const", 0)
            qlats_df = pd.DataFrame(
                qlat_const,
                index=self.segment_index,
                columns=range(nts // qts_subdivisions),
                dtype="float32",
            )

        # TODO: Make a more sophisticated date-based filter
        max_col = 1 + nts // qts_subdivisions
        if len(qlats_df.columns) > max_col:
            qlats_df.drop(qlats_df.columns[max_col:], axis=1, inplace=True)

        if not self.segment_index.empty:
            qlats_df = qlats_df[qlats_df.index.isin(self.segment_index)]

        self._qlateral = qlats_df

    ######################################################################
    #FIXME Temporary solution to hydrofabric issues. Fix specific instances here for now...
    def bandaid(self,):
        #This chunk assigns lake_ids to segments that reside within the waterbody:
        self._dataframe.loc[[5548,5551,],'waterbody'] = '5194634'
        self._dataframe.loc[[5539,5541,5542],'waterbody'] = '5194604'
        self._dataframe.loc[[2710744,2710746],'waterbody'] = '120051895'
        self._dataframe.loc[[1536065,1536067],'waterbody'] = '7100709'
        self._dataframe.loc[[1536104,1536099,1536084,1536094],'waterbody'] = '120052233'
        self._dataframe.loc[[2711040,2711044,2711047],'waterbody'] = '120052275'
    #######################################################################


def read_file(file_name):
    extension = file_name.suffix
    if extension=='.csv':
        df = pd.read_csv(file_name)
    elif extension=='.parquet':
        df = pq.read_table(file_name).to_pandas().reset_index()
        df.index.name = None
    
    return df

def tailwaters(N):
    '''
    Find network tailwaters
    
    Arguments
    ---------
    N (dict, int: [int]): Network connections graph
    
    Returns
    -------
    (iterable): tailwater segments
    
    Notes
    -----
    - If reverse connections graph is handed as input, then function
      will return network headwaters.
      
    '''
    tw = chain.from_iterable(N.values()) - N.keys()
    for m, n in N.items():
        if not n:
            tw.add(m)
    return tw

def reservoir_shore(connections, waterbody_nodes):
    wbody_set = set(waterbody_nodes)
    not_in = lambda x: x not in wbody_set

    shore = set()
    for node in wbody_set:
        shore.update(filter(not_in, connections[node]))
    return list(shore)

def reservoir_boundary(connections, waterbodies, n):
    if n not in waterbodies and n in connections:
        return any(x in waterbodies for x in connections[n])
    return False

def reverse_surjective_mapping(d):
    rd = defaultdict(list)
    for src, dst in d.items():
        rd[dst].append(src)
    rd.default_factory = None
    return rd

def separate_waterbodies(connections, waterbodies):
    waterbody_nodes = {}
    for wb, nodes in reverse_surjective_mapping(waterbodies).items():
        waterbody_nodes[wb] = net = {}
        for n in nodes:
            if n in connections:
                net[n] = list(filter(waterbodies.__contains__, connections[n]))
    return waterbody_nodes

def replace_waterbodies_connections(connections, waterbodies):
    """
    Use a single node to represent waterbodies. The node id is the
    waterbody id. Create a cross walk dictionary that relates lake_ids
    to the terminal segments within the waterbody footprint.
    
    Arguments
    ---------
    - connections (dict):
    - waterbodies (dict): dictionary relating segment linkIDs to the
                          waterbody lake_id that they lie in

    Returns
    -------
    - new_conn  (dict): connections dictionary with waterbodies represented by single nodes. 
                        Waterbody node ids are lake_ids
    - link_lake (dict): cross walk dictionary where keys area lake_ids and values are lists
                        of waterbody tailwater nodes (i.e. the nodes connected to the 
                        waterbody outlet). 
    """
    new_conn = {}
    link_lake = {}
    waterbody_nets = separate_waterbodies(connections, waterbodies)
    rconn = reverse_network(connections)

    for n in connections:
        if n in waterbodies:
            wbody_code = waterbodies[n]
            if wbody_code in new_conn:
                continue

            # get all nodes from waterbody
            wbody_nodes = [k for k, v in waterbodies.items() if v == wbody_code]
            outgoing = reservoir_shore(connections, wbody_nodes)
            new_conn[wbody_code] = outgoing
            
            if len(outgoing)>=1:
                if outgoing[0] in waterbodies:
                    new_conn[wbody_code] = [waterbodies.get(outgoing[0])]
                link_lake[wbody_code] = list(set(rconn[outgoing[0]]).intersection(set(wbody_nodes)))[0]
            else:
                subset_dict = {key: value for key, value in connections.items() if key in wbody_nodes}
                link_lake[wbody_code] = list(tailwaters(subset_dict))[0]

        elif reservoir_boundary(connections, waterbodies, n):
            # one of the children of n is a member of a waterbody
            # replace that child with waterbody code.
            new_conn[n] = []

            for child in connections[n]:
                if child in waterbodies:
                    new_conn[n].append(waterbodies[child])
                else:
                    new_conn[n].append(child)
        else:
            # copy to new network unchanged
            new_conn[n] = connections[n]
    
    return new_conn, link_lake
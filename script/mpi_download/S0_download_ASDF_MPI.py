import os
import time

import obspy
import pandas as pd
import pyasdf
from mpi4py import MPI
from obspy.clients.fdsn import Client

from noisepy.seis import noise_module

"""
This script:
    1) downloads data chunck on your choice of length and Client or pre-compiled station list;
    2) cleans up the traces including gaps, removing instrumental response, downsampling and trim;
    3) saves data into ASDF format;
    4) use MPI to speep up downloading process.

Authors: Marine Denolle (mdenolle@fas.harvard.edu) - 11/16/18,06/08/19
         Chengxin Jiang (chengxin_jiang@fas.harvard.edu) - 02/22/19,07/01/19

Note: segmentation fault while manipulating obspy stream can come from too large memory:
- reduce the inc_day variable

A beginning of NoisePy journey!
"""

###############################
# #####PARAMETER SECTION#######
###############################
tt0 = time.time()

# paths and filenames
rootpath = "./data_download"
direc = os.path.join(rootpath, "RAW_DATA")  # where to store the downloaded data
dlist = os.path.join(direc, "station.lst")  # CSV file for station location info
if not os.path.isdir(direc):
    os.mkdir(direc)

# download parameters
client = Client("GEONET")  # client/data center. see https://docs.obspy.org/packages/obspy.clients.fdsn.html for a list
down_list = False  # download stations from pre-compiled list
oput_CSV = True  # output station.list to a CSV file to be used in later stacking steps
flag = True  # print progress when running the script
NewFreq = 10  # resampling at X samples per seconds
rm_resp = False  # boolean to remove instrumental response
respdir = "none"  # output response directory (has to be given if rm_resp is true and other than inv)
freqmin = 0.05  # pre filtering frequency bandwidth
freqmax = 4

# station information
lamin, lomin, lamax, lomax = (
    -46.5,
    168,
    -38,
    175,
)  # regional box: min lat, min lon, max lat, max lon
dchan = ["HH*"]  # channel if down_list=false
dnet = ["NZ"]  # network
dsta = ["M?Z"]  # station (do either one station or *)
start_date = ["2018_05_01_0_0_0"]  # start date of download
end_date = ["2018_05_08_0_0_0"]  # end date of download
inc_hours = 48  # length of data for each request (in hour)

# time tags
starttime = obspy.UTCDateTime(start_date[0])
endtime = obspy.UTCDateTime(end_date[0])

# assemble parameters for pre-processing
prepro_para = {
    "rm_resp": rm_resp,
    "respdir": respdir,
    "freqmin": freqmin,
    "freqmax": freqmax,
    "samp_freq": NewFreq,
    "start_date": start_date,
    "end_date": end_date,
    "inc_days": inc_hours,
}
metadata = os.path.join(direc, "download_info.txt")
fout = open(metadata, "w")
fout.write(str(prepro_para))
fout.close()

# prepare station info
if down_list:
    if not os.path.isfile(dlist):
        raise IOError("file %s not exist! double check!" % dlist)

    # read station info from list
    locs = pd.read_csv(dlist)
    nsta = len(locs)
    chan = list(locs.iloc[:]["channel"])
    net = list(locs.iloc[:]["network"])
    sta = list(locs.iloc[:]["station"])
    lat = list(locs.iloc[:]["latitude"])
    lon = list(locs.iloc[:]["longitude"])

    # location info: useful for some occasion
    try:
        location = list(locs.iloc[:]["location"])
    except Exception as e:
        print(e)
        location = ["00"] * nsta

else:
    # gather station info
    try:
        inv = client.get_stations(
            network=dnet[0],
            station=dsta[0],
            channel=dchan[0],
            location="*",
            starttime=starttime,
            endtime=endtime,
            minlatitude=lamin,
            maxlatitude=lamax,
            minlongitude=lomin,
            maxlongitude=lomax,
            level="response",
        )
        if flag:
            print(inv)
    except Exception as e:
        print("Abort! " + str(e))
        exit()

    # calculate the total number of channels to download
    sta = []
    net = []
    chan = []
    location = []
    nsta = 0
    for K in inv:
        for sta1 in K:
            for chan1 in sta1:
                sta.append(sta1.code)
                net.append(K.code)
                chan.append(chan1.code)
                location.append(chan1.location_code)
                nsta += 1
    if oput_CSV:
        noise_module.make_stationlist_CSV(inv, direc)


##################################
# ######DOWNLOAD SECTION##########
##################################

# --------MPI---------
comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()

if rank == 0:
    all_chunck = noise_module.get_event_list(start_date[0], end_date[0], inc_hours)
    if all_chunck < 1:
        raise ValueError("Abort! no data chunck between %s and %s" % (start_date[0], end_date[0]))
    splits = len(all_chunck) - 1
else:
    splits, all_chunck = [None for _ in range(2)]

# broadcast the variables
splits = comm.bcast(splits, root=0)
all_chunck = comm.bcast(all_chunck, root=0)
extra = splits % size

# --------MPI: loop through each time chunck--------
for ick in range(rank, splits + size - extra, size):
    if ick < splits:
        s1 = obspy.UTCDateTime(all_chunck[ick])
        s2 = obspy.UTCDateTime(all_chunck[ick + 1])
        date_info = {"starttime": s1, "endtime": s2}

        # filename of the ASDF file
        ff = os.path.join(direc, all_chunck[ick] + "T" + all_chunck[ick + 1] + ".h5")

        # loop through each channel
        for ista in range(nsta):
            # get channel inventory
            sta_inv = client.get_stations(
                network=net[ista],
                station=sta[ista],
                channel=chan[ista],
                starttime=s1,
                endtime=s2,
                location=location[ista],
                level="response",
            )

            with pyasdf.ASDFDataSet(ff, mpi=False, compression="gzip-3") as ds:
                # add the inventory for all components + all time of this tation
                if not ds.waveforms.list():
                    ds.add_stationxml(sta_inv)
                try:
                    # get data
                    t0 = time.time()
                    tr = client.get_waveforms(
                        network=net[ista],
                        station=sta[ista],
                        channel=chan[ista],
                        location=location[ista],
                        starttime=s1,
                        endtime=s2,
                    )
                    t1 = time.time()
                except Exception as e:
                    print(e)
                    continue

                # preprocess to clean data
                tr = noise_module.preprocess_raw(tr, sta_inv, prepro_para, date_info)
                t2 = time.time()

                if len(tr):
                    new_tags = "{0:s}_{1:s}".format(chan[ista].lower(), location[ista].lower())
                    print(new_tags)
                    ds.add_waveforms(tr, tag=new_tags)
                if flag:
                    print(ds)
                    print("downloading data %6.2f s; pre-process %6.2f s" % ((t1 - t0), (t2 - t1)))

tt1 = time.time()
print("downloading step takes %6.2f s" % (tt1 - tt0))
comm.barrier()

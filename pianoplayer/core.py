import csv
import json
import os, sys
import platform
import xml.etree.ElementTree as ET

from music21 import converter

from pianoplayer.hand import Hand
from pianoplayer.scorereader import reader, PIG2Stream, reader_pretty_midi, reader_PIG
import pretty_midi


###########################################################
# Piano Player main analyse and annotate
###########################################################
# def run_analyse():
#     pass
#
#
# def analyse():
#     pass


def run_annotate(filename,
                 outputfile='output.xml',
                 n_measures=100,
                 start_measure=1,
                 depth=0,
                 rbeam=0,
                 lbeam=1,
                 quiet=False,
                 musescore=False,
                 below_beam=False,
                 with_vedo=0,
                 vedo_speed=False,
                 sound_off=False,
                 left_only=False,
                 right_only=False,
                 hand_size_XXS=False,
                 hand_size_XS=False,
                 hand_size_S=False,
                 hand_size_M=False,
                 hand_size_L=False,
                 hand_size_XL=True,
                 hand_size_XXL=False
                 ):
    class Args(object):
        pass
    args = Args()
    args.filename = filename
    args.outputfile = outputfile
    args.n_measures = n_measures
    args.start_measure = start_measure
    args.depth = depth
    args.rbeam = rbeam
    args.lbeam = lbeam
    args.quiet = quiet
    args.musescore = musescore
    args.below_beam = below_beam
    args.with_vedo = with_vedo
    args.vedo_speed = vedo_speed
    args.sound_off = sound_off
    args.left_only = left_only
    args.right_only = right_only
    args.hand_size_XXS = hand_size_XXS
    args.hand_size_XS = hand_size_XS
    args.hand_size_S = hand_size_S
    args.hand_size_M = hand_size_M
    args.hand_size_L = hand_size_L
    args.hand_size_XL = hand_size_XL
    args.hand_size_XXL = hand_size_XXL
    annotate(args)


def annotate_fingers_xml_direct(tree, hand, staff_number, lyrics=False):
    """Annotate fingerings directly in MusicXML file using XML manipulation.
    This avoids music21's round-trip issues with complex tuplets.
    Only annotates key fingering positions (finger changes, stretches, thumb crossings).
    
    Args:
        tree: ElementTree object (parsed XML)
        hand: Hand object with noteseq containing fingering information
        staff_number: 1 for right hand (staff 1), 2 for left hand (staff 2), etc.
                     If 0, annotate all notes regardless of staff
        lyrics: If True, add as lyrics instead of technical fingering
    """
    root = tree.getroot()
    
    note_idx = 0
    prev_fingering = None
    prev_pitch = None
    measure_start = True
    
    # Iterate through all parts and measures
    for part in root.findall('.//part'):
        for measure in part.findall('.//measure'):
            measure_start = True  # Mark start of each measure
            
            for note_elem in measure.findall('.//note'):
                # Check staff number if specified
                if staff_number > 0:
                    staff_elem = note_elem.find('./staff')
                    if staff_elem is not None:
                        if int(staff_elem.text) != staff_number:
                            continue
                
                # Skip if this is a chord note (not the first note of chord)
                if note_elem.find('./chord') is not None:
                    continue
                
                # Skip rests
                if note_elem.find('./rest') is not None:
                    continue
                
                # Get fingering from hand
                if note_idx >= len(hand.noteseq):
                    break
                
                note_data = hand.noteseq[note_idx]
                fingering = note_data.fingering
                current_pitch = note_data.pitch
                note_idx += 1
                
                # Decide whether to show this fingering - only show key positions
                show_fingering = False
                
                # Show first note in each measure for orientation
                if measure_start:
                    show_fingering = True
                    measure_start = False
                # Show thumb (1) transitions - indicates position shift or crossing
                elif fingering == 1 and prev_fingering is not None and prev_fingering > 1:
                    show_fingering = True
                # Show pinky (5) transitions - indicates stretch or position
                elif fingering == 5 and prev_fingering is not None and prev_fingering < 5:
                    show_fingering = True
                # Show large interval jumps (more than octave = 12 semitones)
                elif prev_pitch is not None and abs(current_pitch - prev_pitch) > 12:
                    show_fingering = True
                # Show direction changes with finger changes (e.g., 3->2 when going up)
                elif prev_fingering is not None and prev_pitch is not None:
                    pitch_direction = current_pitch - prev_pitch
                    finger_direction = fingering - prev_fingering
                    # If pitch goes up but fingers go down (or vice versa), it's likely a thumb cross
                    if pitch_direction > 0 and finger_direction < -2:
                        show_fingering = True
                    elif pitch_direction < 0 and finger_direction > 2:
                        show_fingering = True
                
                if show_fingering:
                    fingering_str = str(fingering)
                    
                    if lyrics:
                        # Add as lyric
                        lyric = note_elem.find('./lyric')
                        if lyric is None:
                            lyric = ET.SubElement(note_elem, 'lyric')
                            lyric.set('number', '1')
                        text_elem = lyric.find('./text')
                        if text_elem is None:
                            text_elem = ET.SubElement(lyric, 'text')
                        text_elem.text = fingering_str
                    else:
                        # Add as technical fingering
                        notations = note_elem.find('./notations')
                        if notations is None:
                            notations = ET.SubElement(note_elem, 'notations')
                        
                        technical = notations.find('./technical')
                        if technical is None:
                            technical = ET.SubElement(notations, 'technical')
                        
                        fingering_elem = ET.SubElement(technical, 'fingering')
                        fingering_elem.text = fingering_str
                
                prev_fingering = fingering
                prev_pitch = current_pitch
    
    return tree


def annotate_PIG(hand, is_right=True):
    ans = []
    for n in hand.noteseq:
        onset_time = "{:.4f}".format(n.time)
        offset_time = "{:.4f}".format(n.time + n.duration)
        spelled_pitch = n.pitch
        onset_velocity = str(None)
        offset_velocity = str(None)
        channel = '0' if is_right else '1'
        finger_number = n.fingering if is_right else -n.fingering
        cost = n.cost
        ans.append((onset_time, offset_time, spelled_pitch, onset_velocity, offset_velocity, channel,
                    finger_number, cost, n.noteID))
    return ans


def annotate(args):
    hand_size = 'M'  # default
    if args.hand_size_XXS: hand_size = 'XXS'
    if args.hand_size_XS: hand_size = 'XS'
    if args.hand_size_S: hand_size = 'S'
    if args.hand_size_M: hand_size = 'M'
    if args.hand_size_L: hand_size = 'L'
    if args.hand_size_XL: hand_size = 'XL'
    if args.hand_size_XXL: hand_size = 'XXL'

    xmlfn = args.filename
    if '.msc' in args.filename:
        try:
            xmlfn = str(args.filename).replace('.mscz', '.xml').replace('.mscx', '.xml')
            print('..trying to convert your musescore file to', xmlfn)
            os.system(
                'musescore -f "' + args.filename + '" -o "' + xmlfn + '"')  # quotes avoid problems w/ spaces in filename
            sf = converter.parse(xmlfn)
            if not args.left_only:
                rh_noteseq = reader(sf, beam=args.rbeam)
            if not args.right_only:
                lh_noteseq = reader(sf, beam=args.lbeam)
        except:
            print('Unable to convert file, try to do it from musescore.')
            sys.exit()

    elif '.txt' in args.filename:
        if not args.left_only:
            rh_noteseq = reader_PIG(args.filename, args.rbeam)
        if not args.right_only:
            lh_noteseq = reader_PIG(args.filename, args.lbeam)

    elif '.mid' in args.filename or '.midi' in args.filename or '.xml' in args.filename or '.musicxml' in args.filename:
        # For MIDI files, convert to MusicXML using MuseScore first
        if '.mid' in args.filename or '.midi' in args.filename:
            xmlfn = args.filename.replace('.mid', '.musicxml').replace('.midi', '.musicxml')
            if not os.path.exists(xmlfn):
                print(f'Converting MIDI to MusicXML: {xmlfn}')
                os.system(f'mscore -o "{xmlfn}" "{args.filename}" -platform offscreen 2>/dev/null')
        else:
            # MusicXML file provided directly
            xmlfn = args.filename
        
        # Parse the MusicXML for fingering generation
        sc = converter.parse(xmlfn)
        
        if not args.left_only:
            rh_noteseq = reader(sc, beam=args.rbeam)
        if not args.right_only:
            lh_noteseq = reader(sc, beam=args.lbeam)

    else:
        print(f'Unsupported file format: {args.filename}')
        print('Supported formats: .mid, .midi, .xml, .musicxml, .mscz, .mscx, .txt')
        sys.exit(1)

    if not args.left_only:
        rh = Hand(side="right", noteseq=rh_noteseq, size=hand_size)
        rh.verbose = not (args.quiet)
        if args.depth == 0:
            rh.autodepth = True
        else:
            rh.autodepth = False
            rh.depth = args.depth
        rh.lyrics = args.below_beam

        rh.generate(args.start_measure, args.n_measures)

    if not args.right_only:
        lh = Hand(side="left", noteseq=lh_noteseq, size=hand_size)
        lh.verbose = not (args.quiet)
        if args.depth == 0:
            lh.autodepth = True
        else:
            lh.autodepth = False
            lh.depth = args.depth
        lh.lyrics = args.below_beam

        lh.noteseq = lh_noteseq
        lh.generate(args.start_measure, args.n_measures)

    if args.outputfile is not None:
        ext = os.path.splitext(args.outputfile)[1]
        # an extended PIG file  (note ID) (onset time) (offset time) (spelled pitch) (onset velocity) (offset velocity) (channel) (finger number) (cost)
        if ext == ".txt":
            pig_notes = []
            if not args.left_only:
                pig_notes.extend(annotate_PIG(rh))

            if not args.right_only:
                pig_notes.extend(annotate_PIG(lh, is_right=False))

            with open(args.outputfile, 'wt') as out_file:
                tsv_writer = csv.writer(out_file, delimiter='\t')
                for idx, (onset_time, offset_time, spelled_pitch, onset_velocity, offset_velocity, channel,
                          finger_number, cost, id_n) in enumerate(sorted(pig_notes, key=lambda tup: (float(tup[0]), int(tup[5]), int(tup[2])))):
                    tsv_writer.writerow([idx, onset_time, offset_time, spelled_pitch, onset_velocity, offset_velocity,
                                         channel, finger_number, cost, id_n])
        else:
            # For MusicXML output, use direct XML manipulation to avoid music21 round-trip issues
            # music21 cannot handle complex tuplets and creates "2048th note" errors
            # Annotate directly in XML file
            # For piano scores, staff 1 is right hand (treble), staff 2 is left hand (bass)
            # args.rbeam and args.lbeam are 0 and 1, so we add 1 to get staff numbers
            tree = ET.parse(xmlfn)
            
            if not args.left_only:
                tree = annotate_fingers_xml_direct(tree, rh, args.rbeam + 1, args.below_beam)
            
            if not args.right_only:
                tree = annotate_fingers_xml_direct(tree, lh, args.lbeam + 1, args.below_beam)
            
            # Write output
            tree.write(args.outputfile, encoding='utf-8', xml_declaration=True)

            if args.musescore:  # -m option
                print('Opening musescore with output score:', args.outputfile)
                if platform.system() == 'Darwin':
                    os.system('open "' + args.outputfile + '"')
                else:
                    os.system('musescore "' + args.outputfile + '" > /dev/null 2>&1')
            else:
                print("\nTo visualize annotated score with fingering type:\n musescore '" + args.outputfile + "'")

    if args.with_vedo:
        from pianoplayer.vkeyboard import VirtualKeyboard

        if args.start_measure != 1:
            print('Sorry, start_measure must be set to 1 when -v option is used. Exit.')
            exit()

        vk = VirtualKeyboard(songname=xmlfn)

        if not args.left_only:
            vk.build_RH(rh)
        if not args.right_only:
            vk.build_LH(lh)

        if args.sound_off:
            vk.playsounds = False

        vk.speedfactor = args.vedo_speed
        vk.play()
        vk.vp.show(zoom=2, interactive=1)


if __name__ == '__main__':
    import argparse

    pr = argparse.ArgumentParser(description="""PianoPlayer,
    check out home page https://github.com/marcomusy/pianoplayer""")
    pr.add_argument("filename", type=str, help="Input music xml/midi file name")
    pr.add_argument("-o", "--outputfile", metavar='output.xml', type=str, help="Annotated output xml file name",
                    default='output.xml')
    pr.add_argument("-n", "--n-measures", metavar='', type=int, help="[100] Number of score measures to scan",
                    default=100)
    pr.add_argument("-s", "--start-measure", metavar='', type=int, help="Start from measure number [1]", default=1)
    pr.add_argument("-d", "--depth", metavar='', type=int, help="[auto] Depth of combinatorial search, [4-9]",
                    default=0)
    pr.add_argument("-rbeam", metavar='', type=int, help="[0] Specify Right Hand beam number", default=0)
    pr.add_argument("-lbeam", metavar='', type=int, help="[1] Specify Left Hand beam number", default=1)
    pr.add_argument("--quiet", help="Switch off verbosity", action="store_true")
    pr.add_argument("-m", "--musescore", help="Open output in musescore after processing", action="store_true")
    pr.add_argument("-b", "--below-beam", help="Show fingering numbers below beam line", action="store_true")
    pr.add_argument("-v", "--with-vedo", help="Play 3D scene after processing", action="store_true")
    pr.add_argument("--vedo-speed", metavar='', type=float, help="[1] Speed factor of rendering", default=1.5)
    pr.add_argument("-z", "--sound-off", help="Disable sound", action="store_true")
    pr.add_argument("-l", "--left-only", help="Fingering for left hand only", action="store_true")
    pr.add_argument("-r", "--right-only", help="Fingering for right hand only", action="store_true")
    pr.add_argument("-XXS", "--hand-size-XXS", help="Set hand size to XXS", action="store_true")
    pr.add_argument("-XS", "--hand-size-XS", help="Set hand size to XS", action="store_true")
    pr.add_argument("-S", "--hand-size-S", help="Set hand size to S", action="store_true")
    pr.add_argument("-M", "--hand-size-M", help="Set hand size to M", action="store_true")
    pr.add_argument("-L", "--hand-size-L", help="Set hand size to L", action="store_true")
    pr.add_argument("-XL", "--hand-size-XL", help="Set hand size to XL", action="store_true")
    pr.add_argument("-XXL", "--hand-size-XXL", help="Set hand size to XXL", action="store_true")
    args = pr.parse_args()
    annotate(args)
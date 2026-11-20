// ReSpeaker Core v2.0 screw-free PCB mount, dimensions in mm.
// Place beside enclosure.py. The deck variant uses its original out/deck.stl.
// Set part to "deck", "mount", "gauge", "section" or "assembly".
part = "assembly";
$fn = 72;

board_holes = [[-23.8,14.9],[23.8,14.9],[-29.15,-31.2],[29.15,-31.2]];
floor_z = 4;
board_z = 21;                 // underside of bare PCB; top face at 22.6
board_thickness = 1.6;
hole_d = 3;
socket_top = 12;
foot_depth = 4;               // mount origin is the underside of its plug
mount_z = socket_top - foot_depth;
seat = board_z - mount_z;
eps = 0.04;

module turned(profile) {
    rotate_extrude(convexity=8) polygon(profile);
}

module receiver() {
    // A blind pocket above the continuous acoustic floor. The tapered lip
    // retains the split foot. At full insertion its collar seats on the rim.
    difference() {
        translate([0,0,floor_z-0.12])
            cylinder(h=socket_top-floor_z+0.12, r=3.8);
        translate([0,0,mount_z]) turned([
            [0,-0.4],[2.85,-0.4],[2.85,1.5],[2.45,1.9],
            [2.45,3.45],[3.0,4.0],[3.0,4.0+eps],[0,4.0+eps]]);
    }
}

module rubber_mount() {
    difference() {
        union() {
            // Split anchor bead, insertion stop and spring root.
            turned([[0,0],[2.1,0],[2.70,0.65],[2.70,1.8],[2.25,2.25],
                    [2.25,3.35],[2.90,4],[3.48,4],[3.48,4.6],
                    [1.8,5.45],[0,5.45]]);
            // Two inclined TPU leaves carry axial load by bending. The open
            // window faces sideways and its roof prints from both ends.
            rotate([90,0,0]) linear_extrude(height=2.6,center=true)
                difference() {
                    offset(r=0.30,$fn=24)
                        polygon([[-0.9,4.65],[0.9,4.65],[2.87,8.35],
                                 [2.87,8.75],[0.9,12.25],[-0.9,12.25],
                                 [-2.87,8.75],[-2.87,8.35]]);
                    offset(r=0.23,$fn=24)
                        polygon([[0,6.12],[1.48,8.55],[0,11.02],[-1.48,8.55]]);
                }
            // Board support pad, 2.6 mm locating neck and tapered split barb.
            // Its underside meets the edge of the 3 mm hole at the board top.
            turned([[0,seat-1.9],[1.25,seat-1.9],[3.1,seat-0.10],
                    [3.1,seat],[1.30,seat],[1.30,seat+1.36],
                    [1.80,seat+1.86],[1.80,seat+2.08],[1.22,seat+2.90],
                    [1.08,seat+3.10],[0,seat+3.10]]);
        }
        translate([-.4,-4,-eps]) cube([.8,8,2.85+eps]);
        // This slit starts inside the solid pad, below the PCB. The tips
        // collapse into the slit, so only the TPU touches the drilled hole.
        translate([-.4,-4,seat-.45]) cube([.8,8,4+eps]);
    }
}

module deck() {
    union() {
        difference() {
            import("out/deck.stl",convexity=12);
            for (p=board_holes)
                translate([p.x,p.y,floor_z]) cylinder(r=3.45,h=20);
            // Lower the north port sill to the floor and widen the opening by
            // 1 mm at each end. The nominal clearance below stacked USB is 1.7.
            translate([-21,40,floor_z]) cube([42,12,20]);
        }
        for (p=board_holes) translate([p.x,p.y,0]) receiver();
    }
}

module board_sample() {
    difference() {
        translate([-7,-4,board_z]) cube([14,8,board_thickness]);
        translate([0,0,board_z-eps]) cylinder(d=hole_d,h=board_thickness+2*eps);
    }
}

module fit_gauge() {
    // The ear duplicates the thickness and nominal hole of the board;
    // the socket duplicates the deck. Both can be tried in a short print.
    difference() {
        union() {
            translate([-6,-5.5,0]) cube([12,11,floor_z]);
            translate([5,-4.5,0]) cube([12,9,board_thickness]);
            receiver();
        }
        translate([12,0,-eps]) cylinder(d=hole_d,h=board_thickness+2*eps);
    }
}

module section() {
    // Front half removed through the socket and board; spring shown whole.
    color("DimGray") intersection() {
        union() { receiver(); translate([-5,-4,0]) cube([10,8,floor_z]); }
        translate([-8,0,0]) cube([16,6,25]);
    }
    color("SeaGreen") intersection() {
        board_sample(); translate([-8,0,0]) cube([16,6,25]);
    }
    color("DarkOrange") translate([0,0,mount_z]) rubber_mount();
}

if (part == "deck") deck();
else if (part == "mount") rubber_mount();
else if (part == "gauge") fit_gauge();
else if (part == "section") section();
else if (part == "assembly") {
    color("DimGray") deck();
    for (p=board_holes) color("DarkOrange")
        translate([p.x,p.y,mount_z]) rubber_mount();
}
